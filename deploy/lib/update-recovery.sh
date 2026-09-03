#!/usr/bin/env bash
# Per-VMID single-flight and one-run interrupted-update recovery.
#
# This is deliberately host-local ordinary operational safety. `flock`
# serializes legitimate updater invocations for one VMID, while the small
# journal reconnects a later invocation to the existing run-id-scoped
# rollback state after SIGKILL or host restart. It is not an authenticity
# mechanism and does not attempt to defend against a malicious PVE root.

UPDATE_STATE_DIR=""
UPDATE_LOCK_PATH=""
UPDATE_LOCK_FD=""
UPDATE_JOURNAL_PATH=""
UPDATE_JOURNAL_STATE=""
UPDATE_ROLLBACK_ARMED="0"
UPDATE_INSTALLATION_RUN_ID=""
_UPDATE_STARTUP_RECOVERY_IN_PROGRESS="0"

# --- Filesystem durability barriers (correction pass 9, P1) ----------------
#
# The durable host journal above (update_journal_checkpoint) is not
# sufficient on its own. Recovery material and activated artifacts also
# live on the Hubinet CT filesystem and, for the PVE host package-scan
# helper, the PVE host filesystem itself. A `cp`/`mv`/`rm` returning
# success proves the namespace operation completed in the RUNNING KERNEL --
# it does not by itself prove the data+metadata ordering a later
# transition depends on would survive a subsequent host power loss. The
# one explicit rule applied throughout update-activate.sh: BEFORE
# proceeding past a recovery-critical transition, the filesystem
# containing the state the NEXT transition relies on must have completed a
# durability barrier.
#
# GNU coreutils `sync -f <path>` (Debian/PVE, exactly like the journal's
# own existing use of it above) issues a filesystem synchronization for
# the filesystem CONTAINING <path> -- exactly the granularity every
# invariant below needs, without a daemon, a snapshot, a WAL, or a
# transaction library. A barrier failure is LOAD-BEARING: `die` here is
# deliberate -- fail closed / rollback, never warn-and-continue.

# _update_durability_barrier_ct <path>: durability barrier for the CT
# filesystem containing <path>.
_update_durability_barrier_ct() {
  local path="$1"
  run_logged pct exec "${VMID}" -- sync -f "${path}" \
    || die "durability barrier failed for ${path} inside container ${VMID} -- the preceding state may not survive a power loss; refusing to proceed past this transition"
}

# _update_durability_barrier_host <path>: the same durability barrier for a
# path on the PVE host filesystem itself (never inside pct exec).
#
# HUBINET_OPS_TEST_FAIL_HOST_SYNC (a space-separated list of substrings),
# consulted only when HUBINET_OPS_TEST_MODE=1, is a narrow test-only
# fault-injection seam -- the same escape-hatch convention as
# HUBINET_OPS_TEST_HOST_ROOT in bootstrap-host-control.sh -- letting the
# hermetic test suite exercise this failure path (the run-id suffix in a
# real barrier path is unpredictable ahead of time, so this matches by
# substring, not by exact path) without depending on real filesystem
# permission tricks inside the sandboxed test host root. It is inert
# whenever HUBINET_OPS_TEST_MODE is not "1", so production behavior is
# always the real `sync -f`.
_update_durability_barrier_host() {
  local path="$1" needle
  if [[ "${HUBINET_OPS_TEST_MODE:-0}" == "1" ]]; then
    for needle in ${HUBINET_OPS_TEST_FAIL_HOST_SYNC:-}; do
      [[ -n "${needle}" && "${path}" == *"${needle}"* ]] \
        && die "durability barrier failed for ${path} on the PVE host filesystem (simulated test failure) -- refusing to proceed past this transition"
    done
  fi
  run_logged sync -f "${path}" \
    || die "durability barrier failed for ${path} on the PVE host filesystem -- the preceding state may not survive a power loss; refusing to proceed past this transition"
}

# _update_durability_barrier_ct_or_hard_stop / _host_or_hard_stop: the same
# two barriers above, for use from WITHIN a rollback-restore helper
# (update-activate.sh's _update_rollback_*), which are already
# mid-recovery and report failure via _update_rollback_hard_stop (preserve
# every rollback/backup artifact and the active journal for manual
# recovery) rather than die -- calling die there would `exit` from deep
# inside the EXIT trap's own rollback call, skipping the rest of that
# trap's cleanup, exactly like any other hard stop inside these helpers
# already does today for every other failure they can hit.
_update_durability_barrier_ct_or_hard_stop() {
  local path="$1" context="$2"
  pct exec "${VMID}" -- sync -f "${path}" >/dev/null 2>&1 \
    || _update_rollback_hard_stop "durability barrier failed for ${path} inside container ${VMID} while ${context} -- the restored state may not survive a power loss"
}

_update_durability_barrier_host_or_hard_stop() {
  local path="$1" context="$2" needle
  if [[ "${HUBINET_OPS_TEST_MODE:-0}" == "1" ]]; then
    for needle in ${HUBINET_OPS_TEST_FAIL_HOST_SYNC:-}; do
      [[ -n "${needle}" && "${path}" == *"${needle}"* ]] \
        && _update_rollback_hard_stop "durability barrier failed for ${path} on the PVE host filesystem (simulated test failure) while ${context}"
    done
  fi
  sync -f "${path}" \
    || _update_rollback_hard_stop "durability barrier failed for ${path} on the PVE host filesystem while ${context} -- the restored state may not survive a power loss"
}

# _update_preflight_ct_sync: proves `sync -f` is usable inside this
# container BEFORE any managed-state mutation begins (AGENTS.md task
# prompt correction pass 9, section 3: preflight/prove sync is available
# where required before entering mutation). Uses agent.env -- already
# proven present by update_ownership_verify, and never itself a
# rollback-managed mutation target -- as a bounded, always-live, always
# distinguishable probe target; a no-op barrier, since nothing has changed
# yet.
_update_preflight_ct_sync() {
  run_logged pct exec "${VMID}" -- sync -f /etc/hubinet-ops/agent.env \
    || die "the CT durability barrier (sync -f) is not usable inside container ${VMID} -- refusing to begin the update mutation window without it"
}

_update_state_host_path() {
  _host_control_host_path "/var/lib/hubinet-ops/update-state"
}

update_lock_acquire() {
  UPDATE_STATE_DIR="$(_update_state_host_path)"
  _host_control_install_dir 0700 "${UPDATE_STATE_DIR}" \
    || die "could not create the updater state directory /var/lib/hubinet-ops/update-state"
  UPDATE_LOCK_PATH="${UPDATE_STATE_DIR}/vmid-${VMID}.lock"
  UPDATE_JOURNAL_PATH="${UPDATE_STATE_DIR}/vmid-${VMID}.journal"

  # The open descriptor owns the lease. The stable file is intentionally
  # retained: an unheld file after SIGKILL/reboot is not a stale lock.
  exec {UPDATE_LOCK_FD}>"${UPDATE_LOCK_PATH}"
  if ! flock -n "${UPDATE_LOCK_FD}"; then
    die "another Hubinet Ops updater run owns VMID ${VMID}; no ownership verification, planning, staging, or mutation was attempted"
  fi
  log_info "acquired exclusive updater lease for VMID ${VMID} (${UPDATE_LOCK_PATH})"
}

_update_set_run_paths() {
  [[ -n "${UPDATE_RUN_ID}" ]] || die "internal error: cannot derive updater paths without UPDATE_RUN_ID"
  UPDATE_TOOL_CT_PATH="/tmp/hubinet-ops-authority-tool-${UPDATE_RUN_ID}.py"
  UPDATE_PROBE_CT_PATH="/tmp/hubinet-ops-update-probe-${UPDATE_RUN_ID}.py"
  UPDATE_FENCE_CT_PATH="/tmp/hubinet-ops-update-fence-${UPDATE_RUN_ID}.py"
  UPDATE_CT_SOURCE_TARBALL="/tmp/hubinet-ops-update-src-${UPDATE_RUN_ID}.tar.gz"
  UPDATE_CT_SOURCE_DIR="/tmp/hubinet-ops-update-src-${UPDATE_RUN_ID}"
  UPDATE_APP_STAGED_PATH="/opt/hubinet-ops/app.staged-${UPDATE_RUN_ID}"
  # No staged-virtualenv path exists any more: a changed-requirements
  # update builds the new environment directly at /opt/hubinet-ops/.venv
  # inside the mutation window, because a virtualenv is not relocatable
  # (see _update_activate_venv_and_requirements in update-activate.sh).
  UPDATE_REQUIREMENTS_STAGED_PATH="/opt/hubinet-ops/requirements.txt.staged-${UPDATE_RUN_ID}"
  UPDATE_UNIT_STAGED_PATH="/etc/systemd/system/hubinet-ops.service.staged-${UPDATE_RUN_ID}"
  if [[ -n "${UPDATE_HELPER_PATH:-}" ]]; then
    UPDATE_HELPER_HOST_PATH="$(_host_control_host_path "${UPDATE_HELPER_PATH}")"
    UPDATE_HELPER_STAGED_HOST_PATH="${UPDATE_HELPER_HOST_PATH}.staged-${UPDATE_RUN_ID}"
  fi
}

# _update_is_valid_run_id: accepts exactly the two legal output shapes of
# the shared bootstrap-common.sh::_generate_run_id (PR #65 correction pass
# 13, P2) -- never a broader "any path-safe text" grammar.
#
# Normal path: 32 lowercase hex characters (16 random bytes from
# /dev/urandom, hex-encoded).
#
# Fallback path (used only when /dev/urandom yields nothing, never
# expected on a real Linux PVE host but still a legal generator output):
# exactly "<digits>-<digits>-<digits>" (`${timestamp}-$$-${RANDOM}${RANDOM}`,
# each component numeric).
#
# A run-id journaled by an updater invocation that itself hit the fallback
# path previously failed this function's old ^[0-9a-f]+$-only check, which
# made that journal unrecoverable after SIGKILL/reboot even though the
# updater legitimately created it. Path safety stays load-bearing: no
# slash, dot, whitespace, or other character ever matches either branch.
#
# The normal branch (PR #65 correction pass 14) is exactly the 32
# lowercase-hex characters _generate_run_id's normal path can actually
# produce -- ^[0-9a-f]+$ is broader than that generator can ever emit
# (it also accepts 1, 31, or 33 hex characters, none of them a real
# run-id), so it is tightened to ^[0-9a-f]{32}$ rather than left as a
# generic "looks like hex" check.
_update_is_valid_run_id() {
  local value="$1"
  [[ "${value}" =~ ^[0-9a-f]{32}$ ]] && return 0
  [[ "${value}" =~ ^[0-9]+-[0-9]+-[0-9]+$ ]] && return 0
  return 1
}

_update_journal_marker_is_recovery_relevant() {
  case "$1" in
    update-service-autostart-disable-attempted|\
    update-service-stop-attempted|\
    update-app-activation-attempted|\
    update-venv-activation-attempted|\
    update-unit-activation-attempted|\
    update-helper-activated|\
    update-authority-reset-attempted|\
    update-authority-restored|\
    update-marker-activation-attempted|\
    update-marker-precondition-exists|\
    update-marker-precondition-absent|\
    update-maintenance-fence-held) return 0 ;;
    # Family 1 correction pass: the four package-update boundary markers,
    # kept as their own arm (rather than chained onto the legacy list
    # above) so each existing marker's own line is untouched.
    update-boundary-created|\
    update-boundary-activated|\
    update-boundary-config-activated|\
    update-boundary-journal-created) return 0 ;;
    *) return 1 ;;
  esac
}

# _update_journal_marker_id_is_valid <kind> <id>: typed per-kind id
# validation for every durable journal ledger marker, shared identically by
# update_journal_checkpoint (write) and _update_journal_load (read) so the
# two can never drift (Family 1 correction pass, P1).
#
# The legacy VMID-scoped markers keep requiring id == VMID exactly as
# before -- that is unchanged and never weakened. The package-update
# boundary markers instead validate against the fixed, closed set of ids
# update-boundaries.sh can ever actually record for that SPECIFIC marker
# kind: a bare "id == VMID" would have silently accepted only the
# config-activation marker (whose id genuinely is the VMID) while dropping
# "update-boundary-created snapshot", "update-boundary-activated
# execution", and "update-boundary-journal-created <path>" -- exactly the
# confirmed P1 (a durable-looking update_journal_record call whose marker
# never actually survives to the on-disk journal, so a restart cannot
# reconstruct which boundary artifacts the interrupted run owns).
#
# "update-boundary-staged" is deliberately NOT here and never durable --
# see update-boundaries.sh's own module header for why: its cleanup is
# deterministic from the live path plus the loaded UPDATE_RUN_ID alone, so
# giving it durable journal identity would only grow the journal format
# for no rollback-correctness benefit.
_update_journal_marker_id_is_valid() {
  local kind="$1" id="$2" candidate
  case "${kind}" in
    update-service-autostart-disable-attempted|\
    update-service-stop-attempted|\
    update-app-activation-attempted|\
    update-venv-activation-attempted|\
    update-unit-activation-attempted|\
    update-helper-activated|\
    update-authority-reset-attempted|\
    update-authority-restored|\
    update-marker-activation-attempted|\
    update-marker-precondition-exists|\
    update-marker-precondition-absent|\
    update-maintenance-fence-held|\
    update-boundary-config-activated)
      [[ "${id}" == "${VMID}" ]]
      ;;
    update-boundary-created|update-boundary-activated)
      case "${id}" in
        snapshot|execution|mutation|rollback|health) return 0 ;;
        *) return 1 ;;
      esac
      ;;
    update-boundary-journal-created)
      for candidate in ${UPDATE_BOUNDARY_JOURNAL_DIRS}; do
        [[ "${id}" == "$(_host_control_host_path "${candidate}")" ]] && return 0
      done
      return 1
      ;;
    *) return 1 ;;
  esac
}

# Atomic durable replacement for the bounded one-run journal. Every caller
# invokes this before the destructive transition its newly-recorded marker
# arms. `sync -f` is the ordinary local-filesystem crash boundary; no claim
# is made about hostile storage or controller behavior.
update_journal_checkpoint() {
  local state="$1" tmp kind id
  [[ "${state}" == "active" || "${state}" == "completed" || "${state}" == "recovered" ]] \
    || die "internal error: invalid updater journal state '${state}'"
  [[ -n "${UPDATE_JOURNAL_PATH}" && -n "${UPDATE_RUN_ID}" && -n "${UPDATE_INSTALLATION_RUN_ID}" ]] \
    || die "internal error: updater journal identity was not initialized"

  tmp="$(mktemp "${UPDATE_JOURNAL_PATH}.tmp.XXXXXX")"
  chmod 0600 "${tmp}"
  {
    printf 'format=hubinet-ops-update-journal-v1\n'
    printf 'state=%s\n' "${state}"
    printf 'vmid=%s\n' "${VMID}"
    printf 'run_id=%s\n' "${UPDATE_RUN_ID}"
    printf 'installation_run_id=%s\n' "${UPDATE_INSTALLATION_RUN_ID}"
    printf 'rollback_armed=%s\n' "${UPDATE_ROLLBACK_ARMED}"
    printf 'requirements_changed=%s\n' "${UPDATE_REQUIREMENTS_CHANGED:-0}"
    printf 'authority_action=%s\n' "${UPDATE_AUTHORITY_ACTION:-}"
    printf 'db_backup_path=%s\n' "${UPDATE_DB_BACKUP_PATH:-}"
    if [[ -f "${BOOTSTRAP_LEDGER}" ]]; then
      while read -r kind id; do
        if _update_journal_marker_is_recovery_relevant "${kind}" \
          && _update_journal_marker_id_is_valid "${kind}" "${id}"; then
          printf 'ledger=%s %s\n' "${kind}" "${id}"
        fi
      done <"${BOOTSTRAP_LEDGER}"
    fi
  } >"${tmp}"
  sync -f "${tmp}" \
    || { rm -f -- "${tmp}"; die "could not flush updater journal temporary file for VMID ${VMID}"; }
  mv "${tmp}" "${UPDATE_JOURNAL_PATH}" \
    || { rm -f -- "${tmp}"; die "could not atomically replace updater journal for VMID ${VMID}"; }
  sync -f "${UPDATE_STATE_DIR}" \
    || die "could not flush the updater state directory after journaling VMID ${VMID}"
  UPDATE_JOURNAL_STATE="${state}"
}

update_journal_record() {
  ledger_record "$1" "$2"
  update_journal_checkpoint active
}

_update_journal_clear() {
  # Test-only fault injection (PR #65 correction pass 13, P1):
  # HUBINET_OPS_TEST_FAIL_JOURNAL_CLEAR, consulted only when
  # HUBINET_OPS_TEST_MODE=1, simulates journal-clear itself failing after
  # a terminal (completed/recovered) checkpoint is already durable -- the
  # real `rm` on this host-side path is never faked by the pct-exec fake-
  # command layer, so this is the same kind of narrow seam as
  # HUBINET_OPS_TEST_FAIL_HOST_SYNC above.
  if [[ "${HUBINET_OPS_TEST_MODE:-0}" == "1" && "${HUBINET_OPS_TEST_FAIL_JOURNAL_CLEAR:-0}" == "1" ]]; then
    die "could not remove resolved updater journal ${UPDATE_JOURNAL_PATH} (simulated test failure)"
  fi
  rm -f -- "${UPDATE_JOURNAL_PATH}" \
    || die "could not remove resolved updater journal ${UPDATE_JOURNAL_PATH}"
  sync -f "${UPDATE_STATE_DIR}" \
    || die "could not flush the updater state directory after resolving VMID ${VMID}"
  UPDATE_JOURNAL_STATE=""
}

_update_journal_load() {
  local line key value kind id
  local format="" state="" journal_vmid="" run_id="" installation_run_id=""
  local rollback_armed="" requirements_changed="" authority_action="" db_backup_path=""
  local -A seen=()

  : >"${BOOTSTRAP_LEDGER}"
  while IFS= read -r line || [[ -n "${line}" ]]; do
    [[ "${line}" == *=* ]] \
      || die "interrupted-update journal ${UPDATE_JOURNAL_PATH} is malformed; preserve it and recover VMID ${VMID} manually"
    key="${line%%=*}"
    value="${line#*=}"
    if [[ "${key}" != "ledger" ]]; then
      [[ -z "${seen[${key}]:-}" ]] \
        || die "interrupted-update journal ${UPDATE_JOURNAL_PATH} repeats '${key}'; preserve it and recover manually"
      seen["${key}"]=1
    fi
    case "${key}" in
      format) format="${value}" ;;
      state) state="${value}" ;;
      vmid) journal_vmid="${value}" ;;
      run_id) run_id="${value}" ;;
      installation_run_id) installation_run_id="${value}" ;;
      rollback_armed) rollback_armed="${value}" ;;
      requirements_changed) requirements_changed="${value}" ;;
      authority_action) authority_action="${value}" ;;
      db_backup_path) db_backup_path="${value}" ;;
      ledger)
        kind="${value%% *}"
        id="${value#* }"
        if ! _update_journal_marker_is_recovery_relevant "${kind}" \
          || ! _update_journal_marker_id_is_valid "${kind}" "${id}"; then
          die "interrupted-update journal ${UPDATE_JOURNAL_PATH} has an invalid rollback marker; preserve it and recover manually"
        fi
        ledger_record "${kind}" "${id}"
        ;;
      *) die "interrupted-update journal ${UPDATE_JOURNAL_PATH} has unknown field '${key}'; preserve it and recover manually" ;;
    esac
  done <"${UPDATE_JOURNAL_PATH}"

  [[ "${format}" == "hubinet-ops-update-journal-v1" \
     && ( "${state}" == "active" || "${state}" == "completed" || "${state}" == "recovered" ) \
     && "${journal_vmid}" == "${VMID}" \
     && "${installation_run_id}" =~ ^[0-9A-Za-z_-]+$ \
     && ( "${rollback_armed}" == "0" || "${rollback_armed}" == "1" ) \
     && ( "${requirements_changed}" == "0" || "${requirements_changed}" == "1" ) \
     && ( -z "${authority_action}" || "${authority_action}" == "preserve" || "${authority_action}" == "reset_required" ) ]] \
    || die "interrupted-update journal ${UPDATE_JOURNAL_PATH} failed validation; preserve it and recover VMID ${VMID} manually"
  _update_is_valid_run_id "${run_id}" \
    || die "interrupted-update journal ${UPDATE_JOURNAL_PATH} has an invalid run id; preserve it and recover VMID ${VMID} manually"
  [[ -z "${db_backup_path}" || "${db_backup_path}" == "/var/lib/hubinet-ops/update-backups/${run_id}/authority.db" ]] \
    || die "interrupted-update journal ${UPDATE_JOURNAL_PATH} has an invalid authority backup path; preserve it and recover manually"

  UPDATE_JOURNAL_STATE="${state}"
  UPDATE_RUN_ID="${run_id}"
  UPDATE_INSTALLATION_RUN_ID="${installation_run_id}"
  UPDATE_ROLLBACK_ARMED="${rollback_armed}"
  UPDATE_REQUIREMENTS_CHANGED="${requirements_changed}"
  UPDATE_AUTHORITY_ACTION="${authority_action}"
  UPDATE_DB_BACKUP_PATH="${db_backup_path}"
  UPDATE_HELPER_PATH="/usr/local/libexec/hubinet-package-scan-helper-${UPDATE_INSTALLATION_RUN_ID}"
  _update_set_run_paths
}

# The rollback/recovery boundary. Crossed as soon as this run has
# ATTEMPTED to remove the service's boot activation -- the FIRST mutation
# of the activation window, issued before any service stop -- and
# therefore strictly earlier than the old service-stop-only boundary. A
# non-zero exit after the autostart-disable attempt can never take the
# "the existing installation was never touched" path, which would leave a
# disabled unit behind that no future reboot would ever start.
_update_rollback_boundary_crossed() {
  ledger_has update-service-autostart-disable-attempted "${VMID}" \
    || ledger_has update-service-stop-attempted "${VMID}"
}

# The terminal proof that an installation is coherently in service:
# ENABLED for boot activation, active now, and answering its own
# unauthenticated health probe. Enablement is part of the proof because
# this updater temporarily disables boot activation for its mutation
# window (see _update_disable_service_autostart): an installation that is
# merely active-and-healthy but still disabled would silently fail to come
# back after the next PVE/CT restart, so it is never a recovered,
# completed, or untouched state.
#
# PR #65 correction pass 15, P2: the active+health half of this proof used
# to be a ONE-SHOT request, not a bounded poll. hubinet-ops.service is
# Type=simple, so systemd reports `active` as soon as the process is
# exec'd -- strictly earlier than the moment uvicorn has actually bound
# 127.0.0.1:8787. After a genuine PVE/CT reboot that race is entirely
# ordinary, and a one-shot probe fired at exactly the wrong instant would
# hard-stop startup recovery (or block resolving an already-accepted
# `completed`/`recovered` journal) even though the service becomes healthy
# moments later. Enablement is checked FIRST and remains a single
# positive probe -- it is a static fact about a unit-file symlink, not
# something that becomes true by being waited on, and UNKNOWN/DISABLED
# must fail closed immediately rather than spend any of the readiness
# budget below. Only once enablement is proven does this reuse the exact
# same bounded systemd-active + HTTP-health poll (against the same
# existing BOOTSTRAP_SERVICE_TIMEOUT_SECONDS, itself now bounding its own
# inner curl request -- see _update_wait_until_service_active_and_healthy)
# that forward target activation and rollback already use, rather than a
# third, independent readiness implementation.
_update_prove_service_enabled_active_and_healthy() {
  _update_probe_service_enabled || return 1
  _update_wait_until_service_active_and_healthy
}

# Removes only paths deterministically owned by the loaded run-id. This is
# called only after either rollback restored and proved the old service, or
# a completed/recovered record already proves coherence -- including,
# since PR #65 correction pass 15 (P2), from _update_finish_summary itself
# on the ordinary forward-success path, so a `completed` run's cleanup and
# a later recovery replay's cleanup are the exact same strict, idempotent
# contract rather than two divergent ones.
#
# Every step below is load-bearing: a failure hard-stops (preserving the
# durable journal and every remaining artifact for the next invocation's
# replay) rather than logging and continuing, so a `completed`/`recovered`
# journal is only ever cleared once every run-owned artifact it still
# references has actually been proven removed.
#
# HUBINET_OPS_TEST_FAIL_CT_CLEANUP / HUBINET_OPS_TEST_FAIL_CT_PLAN_TOOL_
# CLEANUP / HUBINET_OPS_TEST_FAIL_HOST_CLEANUP, consulted only when
# HUBINET_OPS_TEST_MODE=1, are narrow test-only fault-injection seams
# (the same convention as HUBINET_OPS_TEST_FAIL_JOURNAL_CLEAR/
# HUBINET_OPS_TEST_FAIL_HOST_SYNC above) letting the hermetic test suite
# exercise each of this function's three independent failure points --
# and their idempotent replay on a later invocation -- without depending
# on real filesystem/transport faults. Inert whenever HUBINET_OPS_TEST_
# MODE is not "1", so production behavior is always the real commands.
_update_cleanup_recovered_run_artifacts() {
  if [[ "${HUBINET_OPS_TEST_MODE:-0}" == "1" && "${HUBINET_OPS_TEST_FAIL_CT_CLEANUP:-0}" == "1" ]]; then
    _update_rollback_hard_stop "could not remove run-owned staged/rollback artifacts for interrupted run ${UPDATE_RUN_ID} (simulated test failure)"
  fi
  pct exec "${VMID}" -- rm -rf \
    "${UPDATE_CT_SOURCE_TARBALL}" \
    "${UPDATE_CT_SOURCE_DIR}" \
    "${UPDATE_APP_STAGED_PATH}" \
    "${UPDATE_REQUIREMENTS_STAGED_PATH}" \
    "${UPDATE_UNIT_STAGED_PATH}" \
    "/opt/hubinet-ops/.hubinet-source-commit.staged-${UPDATE_RUN_ID}" \
    "/opt/hubinet-ops/app.rollback-${UPDATE_RUN_ID}" \
    "/opt/hubinet-ops/.venv.rollback-${UPDATE_RUN_ID}" \
    "/opt/hubinet-ops/requirements.txt.rollback-${UPDATE_RUN_ID}" \
    "/etc/systemd/system/hubinet-ops.service.rollback-${UPDATE_RUN_ID}" \
    "/etc/systemd/system/hubinet-ops.service.rollback-tmp-${UPDATE_RUN_ID}" \
    "/opt/hubinet-ops/.hubinet-source-commit.rollback-${UPDATE_RUN_ID}" \
    >/dev/null 2>&1 \
    || _update_rollback_hard_stop "could not remove run-owned staged/rollback artifacts for interrupted run ${UPDATE_RUN_ID}"
  if [[ "${HUBINET_OPS_TEST_MODE:-0}" == "1" && "${HUBINET_OPS_TEST_FAIL_CT_PLAN_TOOL_CLEANUP:-0}" == "1" ]]; then
    _update_rollback_hard_stop "could not remove run-owned planning/staging tools for interrupted run ${UPDATE_RUN_ID} (simulated test failure)"
  fi
  pct exec "${VMID}" -- rm -f "${UPDATE_TOOL_CT_PATH}" "${UPDATE_PROBE_CT_PATH}" >/dev/null 2>&1 \
    || _update_rollback_hard_stop "could not remove run-owned planning/staging tools for interrupted run ${UPDATE_RUN_ID}"
  # The virtualenv build helper is only ever pushed when requirements
  # actually changed (_update_stage_venv_builder) -- a code-only update
  # never creates it, so this stays bounded to runs that could plausibly
  # have it, rather than issuing an unconditional no-op `rm -f` against a
  # path this run never touched.
  if [[ "${UPDATE_REQUIREMENTS_CHANGED:-0}" == "1" ]]; then
    pct exec "${VMID}" -- rm -f /tmp/hubinet-ops-update-venv-stage.py >/dev/null 2>&1 \
      || _update_rollback_hard_stop "could not remove the run-owned virtualenv build helper for interrupted run ${UPDATE_RUN_ID}"
  fi
  if [[ "${HUBINET_OPS_TEST_MODE:-0}" == "1" && "${HUBINET_OPS_TEST_FAIL_HOST_CLEANUP:-0}" == "1" ]]; then
    _update_rollback_hard_stop "could not remove host-side run-owned helper artifacts for interrupted run ${UPDATE_RUN_ID} (simulated test failure)"
  fi
  # Family 1 correction pass, required cleanup sibling: the five package-
  # update forced-command boundary helpers (deploy/lib/update-boundaries.sh)
  # stage and roll back through the exact same run-owned
  # <live>.staged-${UPDATE_RUN_ID} / <live>.rollback-${UPDATE_RUN_ID} /
  # <live>.restore-tmp-${UPDATE_RUN_ID} host-side naming convention as the
  # package-scan helper immediately below, on the SAME PVE host
  # filesystem. The staged filename is deterministically derived from
  # each boundary's own live path plus this run's own loaded
  # UPDATE_RUN_ID, so this cleanup needs no additional durable journal
  # identity for "staged" itself (see update-boundaries.sh's own module
  # header for why "update-boundary-staged" stays out of the durable
  # journal). Reached from every terminal path that calls this function --
  # forward success (_update_finish_summary), in-process rollback
  # (update_rollback_on_failure), and restart recovery
  # (update_startup_recovery_gate) alike -- so a leftover boundary staging/
  # rollback artifact cannot survive any of the three.
  local _cleanup_boundary_kind _cleanup_boundary_live
  local -a _cleanup_boundary_paths=()
  for _cleanup_boundary_kind in $(_update_boundary_kinds); do
    _cleanup_boundary_live="$(_update_boundary_host_path "${_cleanup_boundary_kind}")"
    _cleanup_boundary_paths+=(
      "${_cleanup_boundary_live}.staged-${UPDATE_RUN_ID}"
      "${_cleanup_boundary_live}.rollback-${UPDATE_RUN_ID}"
      "${_cleanup_boundary_live}.restore-tmp-${UPDATE_RUN_ID}"
    )
  done
  rm -f -- "${UPDATE_HELPER_STAGED_HOST_PATH}" \
    "${UPDATE_HELPER_HOST_PATH}.rollback-${UPDATE_RUN_ID}" \
    "${UPDATE_HELPER_HOST_PATH}.restore-tmp-${UPDATE_RUN_ID}" \
    "${_cleanup_boundary_paths[@]}" \
    || _update_rollback_hard_stop "could not remove host-side run-owned helper artifacts for interrupted run ${UPDATE_RUN_ID}"
}

# _update_recovery_restore_authority_tool (P1, correction pass 7):
# RECOVERY INFRASTRUCTURE -- never a new target deployment plan.
#
# The run-owned authority helper lives at
# /tmp/hubinet-ops-authority-tool-${UPDATE_RUN_ID}.py INSIDE the container,
# and it is pushed exactly once, by update_plan_push_tools during the
# ORIGINAL invocation's Phase U2. A real PVE/CT restart legitimately clears
# the container's volatile /tmp; nothing here ever claims otherwise or
# tries to make /tmp durable. But startup recovery deliberately does NOT
# start a new plan, so without this step nothing would ever restore that
# helper -- and every remaining recovery operation runs THROUGH it:
# _update_ct_path_state's three-valued path probes and the fail-closed
# authority-database remove/restore of a destructive reset. An interrupted
# run whose CT /tmp did not survive could therefore not roll itself back
# at all.
#
# So, before any rollback/path-state/authority-tool operation, re-push the
# SAME bounded updater-owned deploy/lib/hubinet-ops-authority-tool.py to
# the SAME reconstructed run-owned path, then POSITIVELY prove it is
# usable by requiring a definite three-valued (EXISTS/ABSENT) answer for
# one fixed, allowlisted live path. A weaker shell `test -e` is never
# substituted for the helper's own explicit JSON answer.
#
# This step never enters Phase U2, never stages or activates target
# app/config/identity content, never pushes the pre-update HTTP probe
# (recovery does not use it), and is bounded to the previously-loaded run
# id. If the helper cannot be restored or proven usable, it hard stops:
# the journal, every rollback artifact and any authority backup are
# preserved and no new plan is started.
_update_recovery_restore_authority_tool() {
  [[ -n "${UPDATE_RUN_ID}" && -n "${UPDATE_TOOL_CT_PATH}" ]] \
    || _update_rollback_hard_stop "internal error: interrupted-run recovery cannot restore the authority helper without a loaded run identity"
  [[ "${UPDATE_TOOL_CT_PATH}" == "/tmp/hubinet-ops-authority-tool-${UPDATE_RUN_ID}.py" ]] \
    || _update_rollback_hard_stop "internal error: the recovery authority-helper path '${UPDATE_TOOL_CT_PATH}' is not bounded to loaded run ${UPDATE_RUN_ID}"

  run_logged pct push "${VMID}" "${UPDATE_SCRIPT_DIR}/hubinet-ops-authority-tool.py" "${UPDATE_TOOL_CT_PATH}" \
    || _update_rollback_hard_stop "could not restore the run-owned authority helper (${UPDATE_TOOL_CT_PATH}) inside container ${VMID} for interrupted run ${UPDATE_RUN_ID}; no rollback, path-state, or authority-database operation was attempted"

  local probe_rc=0
  _update_ct_path_state /opt/hubinet-ops/app || probe_rc=$?
  (( probe_rc == 0 || probe_rc == 1 )) \
    || _update_rollback_hard_stop "restored the run-owned authority helper for interrupted run ${UPDATE_RUN_ID}, but it did not return a usable path answer inside container ${VMID}; no rollback, path-state, or authority-database operation was attempted"

  log_warn "restored the run-owned authority helper ${UPDATE_TOOL_CT_PATH} for interrupted run ${UPDATE_RUN_ID} (recovery infrastructure only -- no new update plan was started)"
}

update_journal_resolve() {
  local terminal_state="$1"
  update_journal_checkpoint "${terminal_state}"
  _update_cleanup_recovered_run_artifacts
  # Family 3B (correction pass): this run may have acquired the exclusive
  # product-update maintenance fence (_update_acquire_maintenance_fence)
  # before reaching this terminal, untouched-service recovery point -- see
  # this function's sole caller, update-proxmox-0.5.sh's own EXIT trap,
  # for the exact reachable window: a failure or TERM after the fence is
  # durably held but before the rollback boundary is crossed (before
  # _update_disable_service_autostart's first mutation). Release must be
  # positively proven BEFORE the journal carrying this run's recovery
  # identity is discarded, exactly like every other terminal path in this
  # file already does (_update_finish_summary, update_rollback_on_failure,
  # and update_startup_recovery_gate's own two branches below) -- never
  # the reverse: a crash between "journal cleared" and "fence released"
  # would leave a durable fence with no journal left to reconnect it to,
  # permanently refusing every future workload package update. Calling
  # this unconditionally (regardless of whether THIS run's own
  # UPDATE_FENCE_HELD ever became "1") is safe and idempotent: release is
  # keyed off the fence's OWN recorded holder, so a run that never
  # acquired it simply finds it absent or foreign and no-ops.
  _update_test_term_checkpoint before_recovery_fence_release
  _update_release_maintenance_fence
  _update_test_term_checkpoint after_recovery_fence_release
  _update_journal_clear
}

update_startup_recovery_gate() {
  [[ -e "${UPDATE_JOURNAL_PATH}" ]] || return 0
  local detected_state
  _UPDATE_STARTUP_RECOVERY_IN_PROGRESS="1"
  _update_journal_load
  detected_state="${UPDATE_JOURNAL_STATE}"
  log_warn "detected prior updater journal for VMID ${VMID}: run=${UPDATE_RUN_ID}, state=${UPDATE_JOURNAL_STATE}, rollback_armed=${UPDATE_ROLLBACK_ARMED}"

  # Re-run the installation ownership chain in recovery mode. This skips
  # only live payload paths that an interrupted swap may legitimately have
  # moved aside; the CT key marker, forced helper, PVE identity and exact
  # privilege set must still identify the same installation run.
  update_ownership_verify "${VMID}" recovery "${UPDATE_INSTALLATION_RUN_ID}"
  _update_set_run_paths

  if [[ "${UPDATE_JOURNAL_STATE}" == "completed" || "${UPDATE_JOURNAL_STATE}" == "recovered" ]]; then
    _update_prove_service_enabled_active_and_healthy \
      || _update_rollback_hard_stop "run ${UPDATE_RUN_ID} was durably marked ${UPDATE_JOURNAL_STATE}, but VMID ${VMID} does not now prove enabled + active + healthy within ${BOOTSTRAP_SERVICE_TIMEOUT_SECONDS}s (enabled: ${_UPDATE_SERVICE_ENABLED_DETAIL}; readiness: ${_UPDATE_SERVICE_READINESS_DETAIL})"
    # Terminal product state is already durable (this journal's own
    # recorded state), so run-owned cleanup may proceed. Deliberately NOT
    # `update_journal_resolve` here (correction pass, review finding on PR
    # #74): that helper clears the journal itself, and this run's journal
    # is the ONLY durable record carrying its recovery identity
    # (UPDATE_RUN_ID) that a later invocation could match the maintenance
    # fence's own recorded holder against. Releasing the fence THIS run may
    # still hold and clearing that journal must therefore happen in this
    # exact order -- release proven first, journal discarded only after --
    # never the reverse: a crash between "journal cleared" and "fence
    # released" would otherwise leave a durable fence with no journal left
    # to reconnect it to, permanently refusing every future workload update.
    _update_cleanup_recovered_run_artifacts
    # Test-only (PR #74 review finding 2): exercise a real TERM here, after
    # cleanup but before the fence release this journal still owes.
    _update_test_term_checkpoint before_recovery_fence_release
    # The interrupted run was already terminal and the installation now
    # proves enabled + active + healthy, so nothing can still replace
    # backend or helper material on its behalf. Release the fence it may
    # have been holding when it was interrupted -- a release failure
    # propagates (`set -e`, `_UPDATE_STARTUP_RECOVERY_IN_PROGRESS` is still
    # "1" here) straight to the EXIT trap's preserve-and-report path,
    # leaving this journal exactly as it was for the next invocation to
    # retry, rather than falsely reporting cleanup complete.
    _update_release_maintenance_fence
    # Test-only: exercise a real TERM here, after the fence release this
    # journal owed is durably proven but before the journal carrying that
    # proof is discarded -- the exact edge this correction pass closes.
    _update_test_term_checkpoint after_recovery_fence_release
    _update_journal_clear
    _UPDATE_STARTUP_RECOVERY_IN_PROGRESS="0"
    log_warn "previous updater run ${UPDATE_RUN_ID} was already ${detected_state}; final cleanup is complete. Rerun the requested update."
    exit 0
  fi

  if [[ "${UPDATE_ROLLBACK_ARMED}" == "1" ]]; then
    # Rollback below is executed entirely through the run-owned authority
    # helper, which a PVE/CT restart may legitimately have removed with
    # the container's volatile /tmp. Restore and prove it FIRST.
    _update_recovery_restore_authority_tool
    update_rollback_on_failure 1
  else
    # No service stop/destructive transition was armed. Remove only this
    # run's staged artifacts, then positively prove the untouched service.
    _update_cleanup_recovered_run_artifacts
    _update_prove_service_enabled_active_and_healthy \
      || _update_rollback_hard_stop "interrupted run ${UPDATE_RUN_ID} had not armed rollback, but the existing service does not prove enabled + active + healthy"
    update_journal_checkpoint recovered
    _update_test_term_checkpoint before_recovery_fence_release
    # Released only AFTER the untouched installation has been positively
    # proven, not merely because this branch never armed rollback: an
    # unproven service is not a released fence. And -- same ordering as
    # the completed/recovered branch above -- released BEFORE the journal
    # carrying this run's recovery identity is cleared, never after.
    _update_release_maintenance_fence
    _update_test_term_checkpoint after_recovery_fence_release
    _update_journal_clear
  fi

  _UPDATE_STARTUP_RECOVERY_IN_PROGRESS="0"
  log_warn "previous interrupted update run ${UPDATE_RUN_ID} was recovered; no new plan was started. Rerun the updater for VMID ${VMID}."
  exit 0
}
