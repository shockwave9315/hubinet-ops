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

# Each helper's own bounded structural refusal of a request that is not one
# of its typed operations -- the exact same table bootstrap-update-
# boundaries.sh's own _update_boundary_probe_marker uses (duplicated, not
# shared, the same way the two modules already duplicate the kind-name ->
# helper-source-file mapping above: a tiny, stable lookup intrinsic to the
# helper scripts themselves, not to bootstrap or the updater). Used by
# _update_boundary_accept_all below (Family B correction pass) to prove
# each forced-command boundary is genuinely usable before this run may
# declare the target accepted.
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

# _update_boundary_helper_path_state <path>: three-valued, read-only
# classification of a boundary HELPER path on the PVE HOST filesystem
# (Family A correction pass). Prints exactly one of ABSENT / REGULAR /
# UNKNOWN.
#
# update_boundaries_classify used to collapse this into a bare `[[ ! -f
# "${installed_path}" ]]`, which evaluates to the SAME false result
# whether the path is positively absent (ENOENT) or the underlying
# inspection failed for any OTHER reason (EACCES, EIO, ...) -- POSIX test
# cannot tell the two apart. A transient metadata/stat failure on an
# EXISTING helper could therefore still be classified "absent": the
# updater would then plan this boundary as a NEW privileged access path
# and move the staged target directly onto the live path without ever
# preserving the old helper, `_update_boundary_create_key` would then
# refuse (an existing key with no matching helper), and rollback would
# follow the "created" branch and remove the live helper/key as though
# THIS run had provisioned them -- leaving an already-activated
# installation without its configured credentials/helper.
#
# Fixed the same way the CT-side maintenance-fence and authorized_keys
# classifiers already were: delegate to a tiny, bounded, LOCAL python3
# script (this path lives on the PVE host itself -- see
# _update_boundary_host_path -- so no `pct exec` is involved at all,
# unlike _update_boundary_ct_path_state below). os.lstat raises
# FileNotFoundError specifically for ENOENT and any other OSError for
# every other failure -- the one distinction a boolean `[[ -f ]]` cannot
# make.
#
# Deliberately narrower than bootstrap-host-control.sh's own
# _host_control_authorized_keys_path_state: a boundary helper path is
# never a supported symlink target (it is created once, directly, by
# `_host_control_install_file`/`mv`, and never aliased), so a symlink here
# is not a usable answer either way -- ABSENT/REGULAR/UNKNOWN is the whole
# contract, and a symlink (or a directory, device, ...) is UNKNOWN, never
# silently resolved.
_update_boundary_helper_path_state() {
  local path="$1" output status
  # Test-only (Family A correction pass), consulted only when
  # HUBINET_OPS_TEST_MODE=1: a real, genuine stat/EACCES failure on a
  # boundary helper path cannot be modelled through the fake CT/PVE
  # command dispatcher this test suite otherwise relies on, because this
  # classifier runs directly against the PVE HOST filesystem (no `pct
  # exec` involved) in the SAME shared directory
  # (_update_boundary_host_path) that update-ownership.sh's own
  # unrelated, always-checked scan-helper presence proof also lives in --
  # denying that whole directory's permissions to model ONE file's stat
  # failure would trip ownership verification first, on every invocation
  # (forward AND recovery), never reaching this classifier at all. The
  # same narrow HUBINET_OPS_TEST_FAIL_* idiom already used throughout
  # update-recovery.sh (e.g. HUBINET_OPS_TEST_FAIL_CT_CLEANUP) applied
  # here, bounded to exactly the two path shapes this classifier is
  # actually called with: the bare installed/live helper path
  # (update_boundaries_classify, before any mutation), and a preserved
  # rollback copy (".rollback-<run-id>", during update_boundaries_
  # rollback). Inert whenever HUBINET_OPS_TEST_MODE is not "1", so
  # production behavior is always the real os.lstat call below.
  if [[ "${HUBINET_OPS_TEST_MODE:-0}" == "1" ]]; then
    if [[ "${HUBINET_OPS_TEST_FAIL_BOUNDARY_ROLLBACK_COPY_STATE:-0}" == "1" \
      && "${path}" == *.rollback-* ]]; then
      printf 'UNKNOWN'
      return 0
    fi
    if [[ "${HUBINET_OPS_TEST_FAIL_BOUNDARY_INSTALLED_HELPER_STATE:-0}" == "1" \
      && "${path}" != *.rollback-* && "${path}" != *.staged-* ]]; then
      printf 'UNKNOWN'
      return 0
    fi
  fi
  output="$(python3 -c '
import os
import stat
import sys

try:
    st = os.lstat(sys.argv[1])
except FileNotFoundError:
    print("ABSENT")
    sys.exit(0)
except OSError:
    print("UNKNOWN")
    sys.exit(0)

print("REGULAR" if stat.S_ISREG(st.st_mode) else "UNKNOWN")
' "${path}" 2>/dev/null)" && status=0 || status=$?
  if (( status != 0 )); then
    printf 'UNKNOWN'
    return 0
  fi
  case "${output}" in
    ABSENT|REGULAR) printf '%s' "${output}" ;;
    *) printf 'UNKNOWN' ;;
  esac
}

# _update_boundary_ct_path_state <path>: three-valued, read-only existence
# check of a path INSIDE the container (Family A correction pass). Return
# 0=EXISTS, 1=ABSENT, 2=UNKNOWN -- the exact contract and mechanism
# deploy/lib/update-activate.sh's own _update_fence_path_state already
# established for the maintenance fence (inline `python3 -c`, never a
# bare `pct exec ... test -e`, whose boolean answer cannot distinguish
# genuine ENOENT from a metadata/stat failure or an attach/transport
# failure). A new sibling rather than a call to that function directly:
# the fence classifier's own docstring is a fence-specific history: the
# MECHANISM below is identical, but reusing the literal function across an
# unrelated module for an unrelated path would read as though this were
# still about the fence. Used for the boundary private-key existence
# check (_update_boundary_create_key must never guess before generating a
# key that could silently overwrite one already in use) and the
# preserved-configuration-backup existence check during rollback.
_update_boundary_ct_path_state() {
  local path="$1" output status
  output="$(pct exec "${VMID}" -- python3 -c '
import os
import sys

try:
    os.lstat(sys.argv[1])
except FileNotFoundError:
    print("ABSENT")
except OSError:
    print("UNKNOWN")
else:
    print("EXISTS")
' "${path}" 2>/dev/null)" \
    && status=0 || status=$?
  (( status == 0 )) || return 2
  case "${output}" in
    EXISTS) return 0 ;;
    ABSENT) return 1 ;;
    *) return 2 ;;
  esac
}

# ---------------------------------------------------------------------------
# Phase U2 -- classification. Reads only; mutates nothing.
# ---------------------------------------------------------------------------

update_boundaries_classify() {
  local kind installed_tmp target_tmp installed_path installed_state
  for kind in $(_update_boundary_kinds); do
    installed_path="$(_update_boundary_host_path "${kind}")"
    target_tmp="$(secret_tmpfile "/tmp/hubinet-ops-update-boundary.XXXXXX")"
    _update_target_file_to_file "$(_update_boundary_source_name "${kind}")" "${target_tmp}" \
      || die "target commit ${SOURCE_HEAD_SHA} has no $(_update_boundary_source_name "${kind}") -- refusing to plan an update against an unreadable target"
    installed_state="$(_update_boundary_helper_path_state "${installed_path}")"
    case "${installed_state}" in
      ABSENT)
        # A pre-activation installation, or one whose boundary was removed.
        # Provisioning it is a NEW privileged access path, and is tracked
        # and rolled back as one.
        _update_boundary_set_plan "${kind}" absent
        continue
        ;;
      REGULAR) ;;
      *)
        # UNKNOWN (correction pass, Family A): never guessed as absent.
        # Planning against an unproven state could stage a "new"
        # provision over a helper that actually still exists, or classify
        # a genuinely present helper as unchanged/changed from stale
        # content -- either way, mutation must not proceed until this
        # installation's own boundary state can be positively read.
        die "could not positively classify the installed ${kind} boundary helper at ${installed_path} (${installed_state}) -- refusing to plan an update while its state is unproven"
        ;;
    esac
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
  local kind plan live staged rollback_copy journal_dir journal_path journal_state
  for journal_dir in ${UPDATE_BOUNDARY_JOURNAL_DIRS}; do
    journal_path="$(_host_control_host_path "${journal_dir}")"
    # Positively classified (Family A correction pass): see
    # _host_control_dir_state's own docstring -- a false ABSENT here would
    # make this run wrongly claim (and durably record) ownership of a
    # journal directory it did not create.
    journal_state="$(_host_control_dir_state "${journal_path}")"
    case "${journal_state}" in
      ABSENT)
        update_journal_record update-boundary-journal-created "${journal_path}"
        # Test-only (Family 1 correction pass): the journal-created marker is
        # now durable, but the directory it describes does not exist yet --
        # proves a restart can reconstruct that this run owns creating it.
        _update_test_kill_checkpoint "boundary-journal-created-${journal_dir##*/}"
        _host_control_install_dir 0700 "${journal_path}" \
          || die "failed to create the Hubinet operation journal ${journal_dir}"
        ;;
      DIRECTORY) ;;
      *)
        die "could not positively classify the Hubinet operation journal directory ${journal_dir} (${journal_state}) -- refusing to guess whether this run would be creating it before mutation"
        ;;
    esac
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
  local kind="$1" key_path key_state
  key_path="$(_update_boundary_key_path "${kind}")"
  run_logged pct exec "${VMID}" -- install -d -o hubinetops -g hubinetops -m 0700 "${UPDATE_BOUNDARY_CT_DIR}" \
    || die "failed to ensure the host-control directory inside container ${VMID}"
  # Never overwrite an existing private key: a key file already at this path
  # may be the one an existing authorization trusts, and replacing it would
  # silently break that boundary while leaving its authorization in place.
  # Positively classified (Family A correction pass): a bare `test -e`
  # cannot distinguish genuine ENOENT from a metadata/stat failure, and an
  # UNKNOWN here must never be read as "safe to generate a new key" --
  # see _update_boundary_ct_path_state's own docstring.
  # Same errexit-safe idiom used throughout the update-activate.sh sibling
  # this classifier mirrors: capture the exit status without ever leaving
  # this as a bare failing statement under `set -e`.
  _update_boundary_ct_path_state "${key_path}" && key_state=0 || key_state=$?
  case "${key_state}" in
    1) ;; # ABSENT -- safe to generate.
    0) die "the ${kind} boundary private key already exists at ${key_path} inside container ${VMID} but its forced-command helper does not -- refusing to overwrite key material; resolve this manually" ;;
    *) die "could not positively classify the ${kind} boundary private key path ${key_path} inside container ${VMID} -- refusing to generate a new key while its state is unproven" ;;
  esac
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

# The CT's OWN installed backend virtualenv interpreter -- guaranteed to
# have PyYAML (a firm requirements.txt dependency of the installed
# backend), unlike the CT's bare system python3 this updater's OTHER
# CT-side helpers deliberately use because they need no third-party
# library at all (see e.g. _update_boundary_ct_path_state). Semantic YAML
# decoding does, and reusing the SAME library the installed runtime itself
# requires -- rather than adding a new host PyYAML dependency for this
# updater, or hand-rolling a second partial YAML grammar -- is exactly the
# Family D contract deploy/lib/hubinet-ops-update-host-control-fields.py
# exists to satisfy. Whether the currently-installed venv at this exact
# moment is the pre-update or the already-staged target virtualenv is
# immaterial: both pin the identical PyYAML release, so which one answers
# never changes the decoded scalar value.
UPDATE_BOUNDARY_CT_VENV_PYTHON="/opt/hubinet-ops/.venv/bin/python3"

# _update_boundary_read_host_control_fields <package_scan|package_update>
# <config_file>: the four effective host-control fields (host, port, user,
# known_hosts_path), read SEMANTICALLY, as one bounded JSON object (P2
# correction pass -- Family D).
#
# Replaces the previous _update_boundary_config_scalar/_update_boundary_
# effective_host_control_field pair, which scanned the configuration as
# SOURCE TEXT with a line-oriented regex rather than parsing YAML: an
# inline comment (`host: pve.example # primary endpoint`), a quoted `#`,
# or a YAML escape sequence inside a double-quoted scalar was returned
# lexically instead of decoded, so the updater could inherit a different
# string than the runtime's own effective value -- see
# deploy/lib/hubinet-ops-update-host-control-fields.py's own docstring for
# the full contract and default rules.
#
# `config_file` is a LOCAL snapshot -- the SAME byte-identical read the
# caller already took (never re-fetched from the live CT path a second
# time, which would open a TOCTOU between deriving the endpoint and
# whatever this run does with that same snapshot next). It is staged into
# the container next to the reader script, both under this run's own id,
# and both are best-effort removed again whether or not the read
# succeeds -- neither is durable state this run needs to recover.
_update_boundary_read_host_control_fields() {
  local mode="$1" config_file="$2"
  local scratch_ct_path tool_ct_path tool_host_path status output reason detail

  case "${mode}" in
    package_scan|package_update) ;;
    *) die "internal error: unknown host-control field read mode '${mode}'" ;;
  esac
  # Computed HERE, at call time, never as a bare source-time constant:
  # UPDATE_SCRIPT_DIR/UPDATE_RUN_ID are not assigned their real values
  # until well after this module is sourced (UPDATE_RUN_ID in particular
  # only after a run id is generated) -- see update-plan.sh's own
  # UPDATE_PROBE_CT_PATH/UPDATE_FENCE_CT_PATH for the exact same
  # established pattern.
  tool_host_path="${UPDATE_SCRIPT_DIR}/hubinet-ops-update-host-control-fields.py"
  tool_ct_path="/tmp/hubinet-ops-update-host-control-fields-${UPDATE_RUN_ID}.py"
  scratch_ct_path="/tmp/hubinet-ops-update-config-fields-${UPDATE_RUN_ID}.yaml"

  run_logged pct push "${VMID}" "${tool_host_path}" \
    "${tool_ct_path}" \
    || die "failed to stage the host-control field reader inside container ${VMID}"
  if ! run_logged pct push "${VMID}" "${config_file}" "${scratch_ct_path}"; then
    pct exec "${VMID}" -- rm -f "${tool_ct_path}" >/dev/null 2>&1 || true
    die "failed to stage the configuration snapshot for semantic reading inside container ${VMID}"
  fi

  output="$(pct exec "${VMID}" -- "${UPDATE_BOUNDARY_CT_VENV_PYTHON}" \
    "${tool_ct_path}" "${mode}" "${scratch_ct_path}" 2>/dev/null)" \
    && status=0 || status=$?
  pct exec "${VMID}" -- rm -f "${scratch_ct_path}" "${tool_ct_path}" \
    >/dev/null 2>&1 || true
  (( status == 0 )) \
    || die "failed to read ${mode} host-control fields from ${UPDATE_BOUNDARY_CONFIG_PATH} inside container ${VMID}"

  if [[ "$(_update_boundary_json_field "${output}" ok)" != "true" ]]; then
    reason="$(_update_boundary_json_field "${output}" reason)"
    detail="$(_update_boundary_json_field "${output}" detail)"
    case "${reason}" in
      no_host_control_host)
        die "cannot activate the package-update lifecycle: ${UPDATE_BOUNDARY_CONFIG_PATH} has no package_scan.host_control.host and no source.pve_endpoint to derive it from"
        ;;
      pve_endpoint_no_hostname)
        die "cannot activate the package-update lifecycle: source.pve_endpoint (${detail}) in ${UPDATE_BOUNDARY_CONFIG_PATH} has no host-control hostname"
        ;;
      *)
        die "cannot read ${mode} host-control fields from ${UPDATE_BOUNDARY_CONFIG_PATH}: ${reason:-unknown_error}${detail:+ (${detail})}"
        ;;
    esac
  fi
  printf '%s' "${output}"
}

# _update_boundary_json_field <json> <field>: one bounded top-level field
# out of a small JSON object already captured in a shell variable -- never
# a second read of anything, and never YAML. Standard library only, and
# runs on the HOST (never the CT): the object itself already crossed that
# boundary as this function's own plain-text argument.
_update_boundary_json_field() {
  python3 -c '
import json
import sys

data = json.loads(sys.argv[1])
value = data.get(sys.argv[2])
if value is None:
    print("")
elif isinstance(value, bool):
    print("true" if value else "false")
else:
    print(value)
' "$1" "$2"
}

# _update_boundary_yaml_dq_scalar <value>: print <value> as a YAML
# double-quoted scalar that round-trips to the EXACT original string (P2
# correction pass).
#
# host/user/known_hosts_path from _update_boundary_effective_host_control_
# field above are inherited from the installation's OWN existing
# package_scan.host_control configuration (or reproduce its documented
# defaults) -- an already-decoded string this updater does not choose the
# shape of. The runtime config parser (app/inventory_runtime_config.py)
# accepts any non-empty string, including one containing a literal '"' or
# a literal '\'. _update_boundary_activate_config used to interpolate that
# string directly inside a YAML double-quoted scalar (`"${value}"`): a
# literal '"' breaks the quoting (malformed YAML), and a literal '\' is
# processed as a YAML escape inside a double-quoted scalar, silently
# reinterpreting the decoded value into something other than what was
# configured.
#
# JSON's string syntax is a legal YAML double-quoted flow scalar (YAML 1.2
# is a strict superset of JSON), so `json.dumps` of the exact input string
# is sufficient -- no partial hand-rolled escaping, no restriction on
# otherwise-valid existing values, and no path canonicalization. Standard
# library only: no new dependency, and python3 is already unconditionally
# required by this module.
_update_boundary_yaml_dq_scalar() {
  python3 -c '
import json
import sys

print(json.dumps(sys.argv[1]))
' "$1"
}

# The activation block is APPENDED, never merged: it is one self-contained
# top-level YAML key, and appending it leaves every other line of the
# operator's configuration byte-for-byte as it was. The updater still never
# regenerates configuration.
_update_boundary_activate_config() {
  local backup_ct_path="${UPDATE_BOUNDARY_CONFIG_PATH}.rollback-${UPDATE_RUN_ID}"
  local block_tmp merged_tmp current_tmp fields_json
  local endpoint_host endpoint_port endpoint_user endpoint_known_hosts
  local endpoint_host_yaml endpoint_user_yaml endpoint_known_hosts_yaml

  current_tmp="$(secret_tmpfile "/tmp/hubinet-ops-update-config-read.XXXXXX")"
  pct exec "${VMID}" -- cat "${UPDATE_BOUNDARY_CONFIG_PATH}" >"${current_tmp}" \
    || die "failed to read ${UPDATE_BOUNDARY_CONFIG_PATH} from container ${VMID}"
  # Resolved BEFORE anything is preserved or written: a configuration this
  # updater cannot read the endpoint out of must fail before it has changed
  # a byte, not halfway through appending a block. Semantic read (P2
  # correction pass -- Family D): the SAME effective values `parse_r0_
  # runtime_config` would compute for `package_scan.host_control` on this
  # installation's own existing configuration -- an explicit scalar when
  # present, or the identical runtime default when a field is omitted, or
  # host derived from `source.pve_endpoint` -- see
  # _update_boundary_read_host_control_fields's own docstring.
  fields_json="$(_update_boundary_read_host_control_fields package_scan "${current_tmp}")"
  endpoint_host="$(_update_boundary_json_field "${fields_json}" host)"
  endpoint_port="$(_update_boundary_json_field "${fields_json}" port)"
  endpoint_user="$(_update_boundary_json_field "${fields_json}" user)"
  endpoint_known_hosts="$(_update_boundary_json_field "${fields_json}" known_hosts_path)"
  # host/user/known_hosts_path are inherited strings, not this updater's own
  # literal -- see _update_boundary_yaml_dq_scalar's own docstring for why
  # they cannot simply be interpolated inside "${...}" below. port is
  # already the validated integer produced above and needs no quoting.
  endpoint_host_yaml="$(_update_boundary_yaml_dq_scalar "${endpoint_host}")"
  endpoint_user_yaml="$(_update_boundary_yaml_dq_scalar "${endpoint_user}")"
  endpoint_known_hosts_yaml="$(_update_boundary_yaml_dq_scalar "${endpoint_known_hosts}")"

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
    host: ${endpoint_host_yaml}
    port: ${endpoint_port}
    user: ${endpoint_user_yaml}
    known_hosts_path: ${endpoint_known_hosts_yaml}
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
# Phase U5 -- acceptance. Non-mutating; runs after activation has proven the
# target service active and healthy, and before this run may declare the
# target accepted (Family B correction pass).
# ---------------------------------------------------------------------------

# update_boundaries_accept_all: end-to-end, non-mutating proof that every
# one of the five package-update forced-command boundaries is actually
# usable, not merely that their helper/key/authorization files exist.
#
# Fresh bootstrap already proves exactly this property for the boundaries
# it provisions (bootstrap-update-boundaries.sh's own
# _accept_update_boundaries) before ever calling itself done -- but until
# this correction pass, the in-place updater activated or replaced the
# same five boundaries and declared the target accepted without ever
# exercising them. A host whose target helper could not actually run (a
# missing interpreter feature, a broken forced-command mapping, ...) would
# only discover that at the first real operator-triggered package update,
# after durable job/journal ownership had already begun.
#
# Uses the SAME non-mutating mechanism bootstrap uses
# (_host_control_probe_forced_command_boundary, in bootstrap-host-
# control.sh): one deliberately malformed typed request per boundary,
# structurally refused by the helper before it can do anything else. No
# snapshot, package mutation, rollback, or real health evaluation is ever
# triggered.
#
# Unlike bootstrap -- which is establishing the FIRST configuration and so
# always uses its own just-written root@22 default -- this reads the
# endpoint host/port/user/known_hosts_path back from the installation's OWN
# now-live package_update.host_control block (update_boundaries_activate
# has already run: activation writes it explicitly, so there is nothing to
# default here) rather than assuming any of bootstrap's defaults. An
# already-running installation may have an explicit, non-default endpoint,
# and this proves THAT exact configuration, not merely what a fresh install
# would have used.
update_boundaries_accept_all() {
  local config_tmp fields_json endpoint_host endpoint_port endpoint_user endpoint_known_hosts
  local kind key_path marker

  config_tmp="$(secret_tmpfile "/tmp/hubinet-ops-update-boundary-accept.XXXXXX")"
  pct exec "${VMID}" -- cat "${UPDATE_BOUNDARY_CONFIG_PATH}" >"${config_tmp}" \
    || die "could not read ${UPDATE_BOUNDARY_CONFIG_PATH} from container ${VMID} to verify the package-update forced-command boundaries"

  # Semantic read (P2 correction pass -- Family D), never defaulted here:
  # activation always writes all four `package_update.host_control` fields
  # explicitly, so this is proving the just-written value round-trips, not
  # deriving one -- see _update_boundary_read_host_control_fields's own
  # docstring.
  fields_json="$(_update_boundary_read_host_control_fields package_update "${config_tmp}")"
  endpoint_host="$(_update_boundary_json_field "${fields_json}" host)"
  endpoint_port="$(_update_boundary_json_field "${fields_json}" port)"
  endpoint_user="$(_update_boundary_json_field "${fields_json}" user)"
  endpoint_known_hosts="$(_update_boundary_json_field "${fields_json}" known_hosts_path)"
  [[ -n "${endpoint_host}" && -n "${endpoint_port}" && -n "${endpoint_user}" && -n "${endpoint_known_hosts}" ]] \
    || die "cannot verify the package-update forced-command boundaries: ${UPDATE_BOUNDARY_CONFIG_PATH} has no complete package_update.host_control endpoint even though activation just wrote it"

  for kind in $(_update_boundary_kinds); do
    key_path="$(_update_boundary_key_path "${kind}")"
    marker="$(_update_boundary_probe_marker "${kind}")"
    _host_control_probe_forced_command_boundary \
      "${key_path}" "${endpoint_host}" "${endpoint_port}" "${endpoint_user}" "${endpoint_known_hosts}" "${marker}" \
      || die "the ${kind} forced-command SSH boundary did not reject the typed acceptance probe as expected inside container ${VMID} (check the ${kind} key, PVE sshd root public-key policy, and the pinned host key) -- refusing to accept the target installation"
  done
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
    # Positively classified (Family A correction pass): a bare `test -e`
    # cannot distinguish genuine ENOENT from a metadata/stat failure, and
    # both this branch's ABSENT and UNKNOWN outcomes must hard-stop the
    # same way here -- the difference matters only for the diagnostic.
    local backup_state
    _update_boundary_ct_path_state "${backup_ct_path}" && backup_state=0 || backup_state=$?
    case "${backup_state}" in
      0) ;; # EXISTS
      1) _update_rollback_hard_stop "the preserved pre-activation ${UPDATE_BOUNDARY_CONFIG_PATH} (${backup_ct_path}) is absent inside container ${VMID}; restore it manually before retrying -- the activated configuration still names key material this rollback is about to remove" ;;
      *) _update_rollback_hard_stop "the preserved pre-activation ${UPDATE_BOUNDARY_CONFIG_PATH} (${backup_ct_path}) inside container ${VMID} could not be positively classified; resolve this manually before retrying -- the activated configuration still names key material this rollback is about to remove" ;;
    esac
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
      # Positively classified (Family A correction pass): the OLD `[[ -e
      # "${rollback_copy}" ]]` branch selector could not distinguish
      # "genuinely absent" from "could not be inspected" -- a transient
      # stat failure on a rollback_copy that actually EXISTS would fall
      # through to the "already restored" replay branch instead, which
      # only checks whether ${live} is SOME executable regular file. If
      # this is genuinely the first rollback attempt, ${live} still holds
      # the NEW (post-activation) helper at that point -- itself a usable
      # executable regular file -- so that branch would silently accept
      # it as "already restored" and move on, leaving the wrong (target,
      # not pre-update) helper live while rollback believes this boundary
      # is fully undone.
      local rollback_copy_state
      rollback_copy_state="$(_update_boundary_helper_path_state "${rollback_copy}")"
      case "${rollback_copy_state}" in
        REGULAR)
          [[ -s "${rollback_copy}" ]] \
            || _update_rollback_hard_stop "the preserved pre-update ${kind} boundary helper (${rollback_copy}) is empty -- restore it manually before retrying"
          rm -f -- "${restore_tmp}" \
            || _update_rollback_hard_stop "could not clear the run-owned ${kind} boundary restore temporary ${restore_tmp}"
          _host_control_install_file 0755 "${rollback_copy}" "${restore_tmp}" \
            || _update_rollback_hard_stop "could not stage the preserved pre-update ${kind} boundary helper for restoration"
          if ! mv "${restore_tmp}" "${live}" 2>/dev/null; then
            rm -f -- "${restore_tmp}" 2>/dev/null || true
            _update_rollback_hard_stop "could not atomically restore the pre-update ${kind} boundary helper onto ${live}"
          fi
          _update_durability_barrier_host_or_hard_stop "${live}" "restoring the pre-update ${kind} boundary helper"
          ;;
        ABSENT)
          # Replay of an already-restored artifact: genuinely proven
          # absent, so ${live} must already BE the restored pre-update
          # helper (an executable regular file), not merely some
          # executable regular file.
          local live_state
          live_state="$(_update_boundary_helper_path_state "${live}")"
          [[ "${live_state}" == "REGULAR" && -x "${live}" ]] \
            || _update_rollback_hard_stop "the preserved pre-update ${kind} boundary helper is absent and ${live} is not a provably usable executable regular file (${live_state}) -- restore it manually before retrying"
          _update_durability_barrier_host_or_hard_stop "${live}" "replaying the already-restored ${kind} boundary helper"
          ;;
        *)
          _update_rollback_hard_stop "the preserved pre-update ${kind} boundary helper (${rollback_copy}) could not be positively classified -- refusing to guess whether it exists before restoring ${live}"
          ;;
      esac
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
