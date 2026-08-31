#!/usr/bin/env bash
# Phase U3 -- staging. Everything here runs while the OLD service is still
# healthy and untouched; a failure at any point here means the old
# installation is simply left running, exactly as it was. Nothing in this
# file activates a staged artifact -- see update-activate.sh for that.

UPDATE_RUN_ID=""
UPDATE_CT_SOURCE_TARBALL=""
UPDATE_CT_SOURCE_DIR=""
UPDATE_APP_STAGED_PATH=""
UPDATE_REQUIREMENTS_STAGED_PATH=""
UPDATE_UNIT_STAGED_PATH=""
UPDATE_HELPER_STAGED_HOST_PATH=""
UPDATE_HELPER_HOST_PATH=""

update_stage_all() {
  # UPDATE_RUN_ID is generated once, by update-proxmox-0.5.sh itself,
  # before Phase U1 -- shared by the planning-phase tool paths
  # (update-plan.sh) and every staging/activation path below, so every
  # /tmp artifact this invocation touches is run-owned from the start.
  [[ -n "${UPDATE_RUN_ID}" ]] || die "internal error: UPDATE_RUN_ID was not set before update_stage_all"
  _update_set_run_paths

  log_phase "Phase U3: stage target artifacts (run ${UPDATE_RUN_ID})"

  _update_stage_source_tree
  _update_stage_app_payload
  if [[ "${UPDATE_REQUIREMENTS_CHANGED}" == "1" ]]; then
    _update_stage_venv_builder
  fi
  if [[ "${UPDATE_UNIT_CHANGED}" == "1" ]]; then
    _update_stage_unit
  fi
  if [[ "${UPDATE_HELPER_CHANGED}" == "1" ]]; then
    _update_stage_helper
  fi

  log_pass "staging complete"
}

_update_stage_source_tree() {
  local tarball_host
  tarball_host="$(secret_tmpfile "/tmp/hubinet-ops-update-src.XXXXXX.tar.gz")"
  # Not secret; reusing secret_tmpfile for its restrictive mode and
  # guaranteed cleanup on exit.
  run_logged git -C "${SOURCE_DIR}" archive "${SOURCE_HEAD_SHA}" -o "${tarball_host}" \
    || die "git archive of commit ${SOURCE_HEAD_SHA} failed"
  run_logged pct push "${VMID}" "${tarball_host}" "${UPDATE_CT_SOURCE_TARBALL}" \
    || die "failed to push target source tarball into container ${VMID}"
  # UPDATE_CT_SOURCE_DIR is this run's own run-id-suffixed path (never a
  # fixed shared name a previous interrupted/killed run could have left
  # populated) -- still clear it defensively before extracting, so a
  # target archive is never overlaid onto ANY prior tree, even in the
  # practically-impossible event this fresh random run-id path already
  # had something at it.
  run_logged pct exec "${VMID}" -- rm -rf "${UPDATE_CT_SOURCE_DIR}" \
    || die "failed to clear the staging extraction directory inside container ${VMID} before use"
  run_logged pct exec "${VMID}" -- mkdir -p "${UPDATE_CT_SOURCE_DIR}" \
    || die "failed to create staging extraction directory inside container ${VMID}"
  run_logged pct exec "${VMID}" -- tar -xzf "${UPDATE_CT_SOURCE_TARBALL}" -C "${UPDATE_CT_SOURCE_DIR}" \
    || die "failed to extract target source tarball inside container ${VMID}"
  run_logged pct exec "${VMID}" -- rm -f "${UPDATE_CT_SOURCE_TARBALL}" \
    || log_warn "could not remove ${UPDATE_CT_SOURCE_TARBALL} inside the container (non-fatal)"
}

_update_stage_app_payload() {
  run_logged pct exec "${VMID}" -- mkdir -p "${UPDATE_APP_STAGED_PATH}" \
    || die "failed to create staged app payload directory inside container ${VMID}"
  ledger_record update-staged-app "${VMID}"
  run_logged pct exec "${VMID}" -- cp -a "${UPDATE_CT_SOURCE_DIR}/app/." "${UPDATE_APP_STAGED_PATH}/" \
    || die "failed to stage the target application payload inside container ${VMID}"
  run_logged pct exec "${VMID}" -- chown -R hubinetops:hubinetops "${UPDATE_APP_STAGED_PATH}" \
    || die "failed to set ownership on the staged application payload"
  if [[ "${UPDATE_REQUIREMENTS_CHANGED}" == "1" ]]; then
    run_logged pct exec "${VMID}" -- cp "${UPDATE_CT_SOURCE_DIR}/requirements.txt" "${UPDATE_REQUIREMENTS_STAGED_PATH}" \
      || die "failed to stage the target requirements.txt inside container ${VMID}"
    run_logged pct exec "${VMID}" -- chown hubinetops:hubinetops "${UPDATE_REQUIREMENTS_STAGED_PATH}" \
      || die "failed to set ownership on the staged requirements.txt"
  fi
}

UPDATE_VENV_STAGE_TOOL_CT_PATH="/tmp/hubinet-ops-update-venv-stage.py"

# _update_stage_venv_builder (correction pass 8, P1): staging no longer
# BUILDS a virtualenv. A Python virtualenv is not generally relocatable --
# the console entrypoints pip generates embed the absolute interpreter
# path of the environment they were created in, so the old
# "build at .venv.staged-<runid>, then rename onto .venv" design produced
# an activated environment whose own entrypoints pointed at a staging
# pathname that no longer existed (see
# deploy/lib/hubinet-ops-update-venv-stage.py's docstring). The new
# environment is therefore created DIRECTLY at /opt/hubinet-ops/.venv,
# inside the mutation window, by update-activate.sh.
#
# What stays in this phase, while the old service is still healthy and
# untouched, is everything that can still fail harmlessly here: the
# target requirements.txt is staged from the exact approved commit (see
# _update_stage_app_payload), and the small build helper is pushed into
# the container and proven present. A push failure here means the old
# installation is simply left running, exactly as it was -- strictly
# better than discovering it after the service has been stopped.
_update_stage_venv_builder() {
  # Recorded BEFORE the push, so a push that fails part-way is still
  # cleaned up -- and so update_stage_cleanup touches the container only
  # for a run that actually got this far, exactly like every other
  # artifact it handles.
  ledger_record update-staged-venv-tool "${VMID}"
  run_logged pct push "${VMID}" "${UPDATE_SCRIPT_DIR}/hubinet-ops-update-venv-stage.py" "${UPDATE_VENV_STAGE_TOOL_CT_PATH}" \
    || die "failed to push the virtualenv build tool into container ${VMID} -- the ACTIVE virtualenv was never touched"
}

_update_stage_unit() {
  run_logged pct exec "${VMID}" -- cp "${UPDATE_CT_SOURCE_DIR}/deploy/hubinet-ops-0.5.service" "${UPDATE_UNIT_STAGED_PATH}" \
    || die "failed to stage the target systemd unit inside container ${VMID}"
  ledger_record update-staged-unit "${VMID}"
  run_logged pct exec "${VMID}" -- chmod 0644 "${UPDATE_UNIT_STAGED_PATH}" \
    || die "failed to set mode on the staged systemd unit"
}

_update_stage_helper() {
  local helper_tmp
  helper_tmp="$(mktemp /tmp/hubinet-ops-update-helper.XXXXXX)"
  # Read the helper payload from the EXACT approved commit (git show), not
  # the mutable SOURCE_DIR worktree file -- this staging step runs before
  # update-activate.sh's own immediately-before-activation HEAD/clean
  # recheck, so a worktree read here could stage content that was never
  # actually confirmed. `>` writes the blob's exact bytes; no command
  # substitution (which would strip a trailing newline).
  git -C "${SOURCE_DIR}" show "${SOURCE_HEAD_SHA}:deploy/hubinet-package-scan-helper.py" >"${helper_tmp}" 2>/dev/null \
    || { rm -f "${helper_tmp}"; die "failed to read deploy/hubinet-package-scan-helper.py from the exact approved commit ${SOURCE_HEAD_SHA}"; }
  [[ -s "${helper_tmp}" ]] \
    || { rm -f "${helper_tmp}"; die "target commit ${SOURCE_HEAD_SHA} produced an empty deploy/hubinet-package-scan-helper.py -- refusing to stage it"; }
  ledger_record update-staged-helper "${UPDATE_HELPER_STAGED_HOST_PATH}"
  _host_control_install_file 0755 "${helper_tmp}" "${UPDATE_HELPER_STAGED_HOST_PATH}" \
    || { rm -f "${helper_tmp}"; die "failed to stage the target PVE host helper"; }
  rm -f "${helper_tmp}"
}

# update_stage_cleanup: best-effort removal of any staged-but-never-
# activated artifact -- called on the failure path only (a success path
# activates every staged artifact, moving it out of its "staged" name
# entirely). Never touches an artifact that was already activated (those
# are cleaned up separately, only after acceptance succeeds).
update_stage_cleanup() {
  _update_cleanup_plan_tools
  if ledger_has update-staged-helper "${UPDATE_HELPER_STAGED_HOST_PATH}" \
    && ! ledger_has update-helper-activated "${VMID}"; then
    rm -f "${UPDATE_HELPER_STAGED_HOST_PATH}" 2>/dev/null || true
  fi
  if [[ -n "${VMID:-}" ]]; then
    # UPDATE_CT_SOURCE_DIR is only assigned once update_stage_all actually
    # runs (it is run-id-suffixed -- see update_stage_all); this cleanup
    # function is also called on a failure BEFORE staging ever started
    # (e.g. Phase U2 classification, plan confirmation), where it is still
    # the empty string default. Never pass an empty path to `rm -rf`.
    if [[ -n "${UPDATE_CT_SOURCE_DIR}" ]]; then
      pct exec "${VMID}" -- rm -rf "${UPDATE_CT_SOURCE_DIR}" >/dev/null 2>&1 || true
    fi
    if ledger_has update-staged-app "${VMID}" && ! ledger_has update-app-activated "${VMID}"; then
      pct exec "${VMID}" -- rm -rf "${UPDATE_APP_STAGED_PATH}" "${UPDATE_REQUIREMENTS_STAGED_PATH}" >/dev/null 2>&1 || true
    fi
    # No staged virtualenv exists to clean up any more -- the target
    # environment is built directly at the live path inside the mutation
    # window, and a partial one is removed by rollback's own
    # state-inspection restore, never here. Only the pushed build helper
    # is this phase's own leftover, and only for a run that actually
    # pushed it (this function also runs on failure paths that never
    # reached staging at all, where it must touch the container for
    # nothing).
    if ledger_has update-staged-venv-tool "${VMID}"; then
      pct exec "${VMID}" -- rm -f "${UPDATE_VENV_STAGE_TOOL_CT_PATH}" >/dev/null 2>&1 || true
    fi
    if ledger_has update-staged-unit "${VMID}" && ! ledger_has update-unit-activated "${VMID}"; then
      pct exec "${VMID}" -- rm -f "${UPDATE_UNIT_STAGED_PATH}" >/dev/null 2>&1 || true
    fi
  fi
}
