#!/usr/bin/env bash
# Phase U4 -- activation, acceptance, and coherent rollback.
#
# From _update_recheck_source_commit onward this file may mutate managed
# installation state. The activation mutation order in
# update_activate_and_accept is fixed and never reordered (AGENTS.md task
# prompt section 20). Every step records a ledger marker (bootstrap-
# common.sh::ledger_record) BEFORE or immediately after the mutation it
# guards, exactly like deploy/bootstrap-proxmox-0.5.sh's own rollback
# convention -- update-proxmox-0.5.sh's own EXIT trap calls
# update_rollback_on_failure whenever the process exits non-zero after the
# service was stopped (ledger_has update-service-stopped), never before.

UPDATE_DB_BACKUP_PATH=""
UPDATE_POST_BACKEND_INSTANCE_ID=""
UPDATE_PRE_NFTABLES_CONF=""
UPDATE_DISPLAY_NAME=""

_update_read_display_name() {
  local inventory_text line raw
  inventory_text="$(pct exec "${VMID}" -- cat /etc/hubinet-ops/inventory.yaml 2>/dev/null)"
  [[ -n "${inventory_text}" ]] \
    || die "could not read inventory.yaml from container ${VMID}"
  line="$(printf '%s\n' "${inventory_text}" | grep '^  display_name:' | head -n1)"
  raw="${line#*display_name:}"
  raw="$(printf '%s' "${raw}" | sed -E 's/^[[:space:]]*"?//; s/"?[[:space:]]*$//')"
  [[ -n "${raw}" ]] || die "could not read source.display_name from container ${VMID}'s inventory.yaml"
  UPDATE_DISPLAY_NAME="${raw}"
}

_update_capture_pre_mutation_facts() {
  _update_read_display_name
  UPDATE_PRE_NFTABLES_CONF="$(pct exec "${VMID}" -- cat /etc/nftables.conf 2>/dev/null)"
  [[ -n "${UPDATE_PRE_NFTABLES_CONF}" ]] \
    || die "could not read /etc/nftables.conf from container ${VMID} before update"
}

# _update_recheck_source_commit: immediately-before-activation re-check,
# mirroring deploy/lib/bootstrap-deploy.sh::phase8_deploy_source's own
# recheck block exactly (same invariant: the approved target must still be
# HEAD, on a still-clean worktree, right before it is deployed).
_update_recheck_source_commit() {
  local recheck_sha
  recheck_sha="$(git -C "${SOURCE_DIR}" rev-parse HEAD 2>/dev/null)" \
    || die "could not re-verify SOURCE_DIR HEAD before activation"
  [[ "${recheck_sha}" == "${SOURCE_HEAD_SHA}" ]] \
    || die "SOURCE_DIR HEAD changed from the confirmed commit (${SOURCE_HEAD_SHA}) to ${recheck_sha} between confirmation and activation -- refusing to activate an unconfirmed commit"
  local recheck_dirty
  recheck_dirty="$(git -C "${SOURCE_DIR}" status --porcelain 2>/dev/null)"
  [[ -z "${recheck_dirty}" ]] \
    || die "SOURCE_DIR became dirty between confirmation and activation -- refusing to activate"
}

update_activate_and_accept() {
  log_phase "Phase U4: activate"

  _update_recheck_source_commit
  _update_capture_pre_mutation_facts

  run_logged pct exec "${VMID}" -- systemctl stop hubinet-ops \
    || die "failed to stop hubinet-ops inside container ${VMID} -- the old installation was never mutated"
  local waited=0 state=""
  while (( waited < BOOTSTRAP_SERVICE_TIMEOUT_SECONDS )); do
    state="$(pct exec "${VMID}" -- systemctl is-active hubinet-ops 2>/dev/null || true)"
    [[ "${state}" != "active" ]] && break
    sleep 1
    waited=$(( waited + 1 ))
  done
  [[ "${state}" != "active" ]] \
    || die "hubinet-ops did not stop within ${BOOTSTRAP_SERVICE_TIMEOUT_SECONDS}s inside container ${VMID}"
  # From this ledger marker onward, a non-zero exit triggers
  # update_rollback_on_failure via the EXIT trap.
  ledger_record update-service-stopped "${VMID}"

  # Step 4 -- activate app payload atomically.
  run_logged pct exec "${VMID}" -- mv /opt/hubinet-ops/app "/opt/hubinet-ops/app.rollback-${UPDATE_RUN_ID}" \
    || die "failed to move the live application payload aside inside container ${VMID}"
  run_logged pct exec "${VMID}" -- mv "${UPDATE_APP_STAGED_PATH}" /opt/hubinet-ops/app \
    || die "failed to activate the staged application payload inside container ${VMID}"
  ledger_record update-app-activated "${VMID}"

  # 5/6. requirements + venv, only if changed.
  if [[ "${UPDATE_REQUIREMENTS_CHANGED}" == "1" ]]; then
    run_logged pct exec "${VMID}" -- mv /opt/hubinet-ops/.venv "/opt/hubinet-ops/.venv.rollback-${UPDATE_RUN_ID}" \
      || die "failed to move the active virtualenv aside inside container ${VMID}"
    run_logged pct exec "${VMID}" -- mv "${UPDATE_VENV_STAGED_PATH}" /opt/hubinet-ops/.venv \
      || die "failed to activate the staged virtualenv inside container ${VMID}"
    run_logged pct exec "${VMID}" -- mv /opt/hubinet-ops/requirements.txt "/opt/hubinet-ops/requirements.txt.rollback-${UPDATE_RUN_ID}" \
      || die "failed to move the active requirements.txt aside inside container ${VMID}"
    run_logged pct exec "${VMID}" -- mv "${UPDATE_REQUIREMENTS_STAGED_PATH}" /opt/hubinet-ops/requirements.txt \
      || die "failed to activate the staged requirements.txt inside container ${VMID}"
    ledger_record update-venv-activated "${VMID}"
  fi

  # Step 7 -- systemd unit, only if changed.
  if [[ "${UPDATE_UNIT_CHANGED}" == "1" ]]; then
    run_logged pct exec "${VMID}" -- cp /etc/systemd/system/hubinet-ops.service "/etc/systemd/system/hubinet-ops.service.rollback-${UPDATE_RUN_ID}" \
      || die "failed to preserve the active systemd unit inside container ${VMID}"
    run_logged pct exec "${VMID}" -- mv "${UPDATE_UNIT_STAGED_PATH}" /etc/systemd/system/hubinet-ops.service \
      || die "failed to activate the staged systemd unit inside container ${VMID}"
    ledger_record update-unit-activated "${VMID}"
    run_logged pct exec "${VMID}" -- systemctl daemon-reload \
      || die "systemctl daemon-reload failed inside container ${VMID}"
  fi

  # Step 8 -- PVE host helper, only if changed (host-side, same path).
  if [[ "${UPDATE_HELPER_CHANGED}" == "1" ]]; then
    cp "${UPDATE_HELPER_HOST_PATH}" "${UPDATE_HELPER_HOST_PATH}.rollback-${UPDATE_RUN_ID}" \
      || die "failed to preserve the active PVE host helper before activation"
    ledger_record update-helper-activated "${VMID}"
    mv "${UPDATE_HELPER_STAGED_HOST_PATH}" "${UPDATE_HELPER_HOST_PATH}" \
      || die "failed to activate the staged PVE host helper (same-path atomic rename)"
  fi

  # Step 9 -- authority action: preserve, or backup + reset.
  if [[ "${UPDATE_AUTHORITY_ACTION}" == "reset_required" ]]; then
    _update_perform_authority_reset
  fi

  # Step 10 -- start service.
  run_logged pct exec "${VMID}" -- systemctl start hubinet-ops \
    || die "failed to start hubinet-ops inside container ${VMID} after activation"
  ledger_record update-service-started "${VMID}"

  waited=0
  state=""
  while (( waited < BOOTSTRAP_SERVICE_TIMEOUT_SECONDS )); do
    state="$(pct exec "${VMID}" -- systemctl is-active hubinet-ops 2>/dev/null || true)"
    [[ "${state}" == "active" ]] && break
    sleep 1
    waited=$(( waited + 1 ))
  done
  [[ "${state}" == "active" ]] \
    || die "hubinet-ops did not become active within ${BOOTSTRAP_SERVICE_TIMEOUT_SECONDS}s after activation (last state: ${state:-unknown})"

  log_pass "activation complete"

  log_phase "Phase U5: acceptance"
  _update_accept_discovery
  _update_accept_host_control
  _update_accept_firewall
  log_pass "acceptance"

  _update_write_source_marker
  _update_finish_summary
}

_update_perform_authority_reset() {
  local backup_dir backup_ct_path tool_output status
  backup_dir="/var/lib/hubinet-ops/update-backups/${UPDATE_RUN_ID}"
  backup_ct_path="${backup_dir}/authority.db"
  run_logged pct exec "${VMID}" -- install -d -o hubinetops -g hubinetops -m 0750 "${backup_dir}" \
    || die "failed to create the authority backup directory inside container ${VMID}"

  tool_output="$(pct exec "${VMID}" -- python3 "${UPDATE_TOOL_CT_PATH}" backup \
    /var/lib/hubinet-ops/authority.db "${backup_ct_path}" \
    "${UPDATE_CURRENT_SCHEMA_MARKER}" "${UPDATE_CURRENT_SCHEMA_VERSION}" "${UPDATE_CURRENT_BACKEND_INSTANCE_ID}" \
    2>/dev/null)" && status=0 || status=$?
  if (( status != 0 )) || ! _json_bool_field_is_true "${tool_output}" "ok"; then
    local reason
    reason="$(_json_field_from_text "${tool_output}" "reason")"
    die "authority database backup failed (${reason:-unknown}) before any live data was removed -- the old authority database remains authoritative and untouched"
  fi
  UPDATE_DB_BACKUP_PATH="${backup_ct_path}"
  ledger_record update-authority-backed-up "${VMID}"

  tool_output="$(pct exec "${VMID}" -- python3 "${UPDATE_TOOL_CT_PATH}" remove /var/lib/hubinet-ops/authority.db 2>/dev/null)" \
    && status=0 || status=$?
  (( status == 0 )) && _json_bool_field_is_true "${tool_output}" "ok" \
    || die "failed to remove the old authority database inside container ${VMID} after a validated backup was already made at ${UPDATE_DB_BACKUP_PATH}"
  ledger_record update-authority-reset "${VMID}"
}

_update_accept_discovery() {
  run_logged pct push "${VMID}" "${BOOTSTRAP_SCRIPT_DIR}/lib/hubinet-ops-bootstrap-accept.py" "${CT_ACCEPT_SCRIPT_CT}" \
    || die "failed to push the acceptance script into container ${VMID}"

  local -a accept_args=("${UPDATE_DISPLAY_NAME}" "${BOOTSTRAP_DISCOVERY_TIMEOUT_SECONDS}")
  if [[ "${UPDATE_AUTHORITY_ACTION}" == "preserve" && "${UPDATE_PRE_COMMITTED_SEQUENCE}" =~ ^[0-9]+$ ]]; then
    accept_args+=("${UPDATE_PRE_COMMITTED_SEQUENCE}")
  fi

  local output status
  output="$(pct exec "${VMID}" -- python3 "${CT_ACCEPT_SCRIPT_CT}" "${accept_args[@]}" 2>&1)" && status=0 || status=$?
  pct exec "${VMID}" -- rm -f "${CT_ACCEPT_SCRIPT_CT}" >/dev/null 2>&1 || true

  printf '%s\n' "${output}" | while IFS= read -r line; do log_info "acceptance: ${line}"; done
  local last_line
  last_line="$(printf '%s\n' "${output}" | tail -n1)"
  [[ "${status}" -eq 0 && "${last_line}" == PASS* ]] \
    || die "post-update discovery acceptance failed: ${last_line:-no output}"

  UPDATE_POST_BACKEND_INSTANCE_ID="$(printf '%s' "${last_line}" | sed -n 's/.*backend_instance_id=\([^ ]*\).*/\1/p')"
  [[ -n "${UPDATE_POST_BACKEND_INSTANCE_ID}" ]] \
    || die "post-update acceptance did not report a backend_instance_id"

  if [[ "${UPDATE_AUTHORITY_ACTION}" == "preserve" ]]; then
    [[ "${UPDATE_POST_BACKEND_INSTANCE_ID}" == "${UPDATE_PRE_BACKEND_INSTANCE_ID}" ]] \
      || die "identity regression: backend_instance_id changed from ${UPDATE_PRE_BACKEND_INSTANCE_ID} to ${UPDATE_POST_BACKEND_INSTANCE_ID} on a schema-preserving update"
  else
    [[ "${UPDATE_POST_BACKEND_INSTANCE_ID}" != "${UPDATE_PRE_BACKEND_INSTANCE_ID}" ]] \
      || die "authority reset did not actually produce a new backend_instance_id"
  fi
}

# _update_accept_host_control: re-runs the same deliberately-unknown typed
# probe deploy/lib/bootstrap-host-control.sh's phase8c already performs at
# install time, proving the forced-command boundary (same helper path,
# same pinned key/known_hosts, unchanged or freshly-activated content)
# still rejects an unknown operation exactly as expected.
_update_accept_host_control() {
  local probe_output probe_status probe_request
  probe_request='{"request_version":1,"operation":"probe","target":{},"context":{}}'
  probe_output="$(printf '%s' "${probe_request}" | pct exec "${VMID}" -- runuser -u hubinetops -- \
    ssh -T -p 22 -i "${HOST_CONTROL_CT_PRIVATE_KEY}" \
    -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
    -o "UserKnownHostsFile=${HOST_CONTROL_CT_KNOWN_HOSTS}" \
    -o PasswordAuthentication=no -o KbdInteractiveAuthentication=no \
    -o ForwardAgent=no -o ClearAllForwardings=yes \
    "root@$(_endpoint_host_of_helper)" 2>/dev/null)" && probe_status=0 || probe_status=$?
  (( probe_status == 2 )) \
    && printf '%s' "${probe_output}" | grep -qF 'unknown host-control operation' \
    || die "post-update host-control acceptance failed: the forced-command SSH boundary did not reject the typed probe as expected"
}

# _endpoint_host_of_helper: the host-control SSH target is always this PVE
# host itself (root@<pve-endpoint-host>) -- read from the CT's own pinned
# known_hosts file rather than requiring a bootstrap-only --pve-endpoint
# flag this updater deliberately does not accept.
_endpoint_host_of_helper() {
  local known_hosts_text
  known_hosts_text="$(pct exec "${VMID}" -- cat "${HOST_CONTROL_CT_KNOWN_HOSTS}" 2>/dev/null)"
  printf '%s\n' "${known_hosts_text}" | head -n1 | cut -d' ' -f1
}

_update_accept_firewall() {
  local post_conf
  post_conf="$(pct exec "${VMID}" -- cat /etc/nftables.conf 2>/dev/null)"
  [[ "${post_conf}" == "${UPDATE_PRE_NFTABLES_CONF}" ]] \
    || die "post-update firewall acceptance failed: /etc/nftables.conf is no longer byte-identical to its pre-update content"
  local nft_active
  nft_active="$(pct exec "${VMID}" -- systemctl is-active nftables 2>/dev/null || true)"
  [[ "${nft_active}" == "active" ]] \
    || die "post-update firewall acceptance failed: nftables is not active inside container ${VMID} (${nft_active:-unknown})"
}

_update_write_source_marker() {
  local marker_tmp
  marker_tmp="$(mktemp /tmp/hubinet-ops-update-source-marker.XXXXXX)"
  printf '%s\n' "${SOURCE_HEAD_SHA}" >"${marker_tmp}"
  run_logged pct push "${VMID}" "${marker_tmp}" /opt/hubinet-ops/.hubinet-source-commit \
    || die "failed to write the installed-source marker after a successful update"
  rm -f "${marker_tmp}"
  run_logged pct exec "${VMID}" -- chown hubinetops:hubinetops /opt/hubinet-ops/.hubinet-source-commit \
    || die "failed to set ownership on the installed-source marker"
}

_update_finish_summary() {
  # Success: clean up rollback material and staged leftovers -- nothing
  # here is managed state a future update depends on.
  pct exec "${VMID}" -- rm -rf \
    "/opt/hubinet-ops/app.rollback-${UPDATE_RUN_ID}" \
    "/opt/hubinet-ops/.venv.rollback-${UPDATE_RUN_ID}" \
    "/opt/hubinet-ops/requirements.txt.rollback-${UPDATE_RUN_ID}" \
    "/etc/systemd/system/hubinet-ops.service.rollback-${UPDATE_RUN_ID}" \
    "${UPDATE_CT_SOURCE_DIR}" \
    >/dev/null 2>&1 || true
  if [[ "${UPDATE_HELPER_CHANGED}" == "1" ]]; then
    rm -f "${UPDATE_HELPER_HOST_PATH}.rollback-${UPDATE_RUN_ID}" 2>/dev/null || true
  fi

  cat <<SUMMARY

Hubinet Ops in-place update: PASS

VMID:                 ${VMID}
Installed source:     ${UPDATE_INSTALLED_SHA} -> ${SOURCE_HEAD_SHA}
backend_instance_id:  ${UPDATE_POST_BACKEND_INSTANCE_ID}
Authority action:     ${UPDATE_AUTHORITY_ACTION}
SUMMARY
  if [[ -n "${UPDATE_DB_BACKUP_PATH}" ]]; then
    cat <<BACKUP
Authority DB backup:  ${UPDATE_DB_BACKUP_PATH} (inside container ${VMID}; retained, not auto-deleted)
Home Assistant:       re-enrollment REQUIRED (backend_instance_id changed)
BACKUP
  else
    cat <<PRESERVED
Home Assistant:       no action required (backend_instance_id preserved)
PRESERVED
  fi
}

# ---------------------------------------------------------------------------
# Rollback -- AGENTS.md task prompt sections 21/24/25. Restores the
# coherent PRE-UPDATE installation, including the authority database when a
# destructive reset was already performed. Never leaves old code paired
# with a new/incompatible schema.
# ---------------------------------------------------------------------------

update_rollback_on_failure() {
  local exit_code="$1"
  log_warn "update failed (exit ${exit_code}) after the service was stopped -- rolling back to the coherent pre-update installation"

  if ledger_has update-service-started "${VMID}"; then
    pct exec "${VMID}" -- systemctl stop hubinet-ops >/dev/null 2>&1 \
      || log_warn "could not stop the newly-started (failed) service during rollback"
  fi

  if ledger_has update-authority-reset "${VMID}"; then
    local remove_output
    remove_output="$(pct exec "${VMID}" -- python3 "${UPDATE_TOOL_CT_PATH}" remove /var/lib/hubinet-ops/authority.db 2>/dev/null)"
    if ! _json_bool_field_is_true "${remove_output}" "ok"; then
      log_warn "could not remove the newly-created target authority database during rollback -- see manual remediation below"
    fi
    if ! pct exec "${VMID}" -- cp "${UPDATE_DB_BACKUP_PATH}" /var/lib/hubinet-ops/authority.db >/dev/null 2>&1; then
      _update_rollback_hard_stop "could not restore the pre-update authority database backup (${UPDATE_DB_BACKUP_PATH}) to /var/lib/hubinet-ops/authority.db -- the backup itself is preserved and untouched; restore it manually before restarting the service"
    fi
    pct exec "${VMID}" -- chown hubinetops:hubinetops /var/lib/hubinet-ops/authority.db >/dev/null 2>&1 || true
    pct exec "${VMID}" -- chmod 0640 /var/lib/hubinet-ops/authority.db >/dev/null 2>&1 || true
  fi

  if ledger_has update-helper-activated "${VMID}"; then
    if ! mv "${UPDATE_HELPER_HOST_PATH}.rollback-${UPDATE_RUN_ID}" "${UPDATE_HELPER_HOST_PATH}" 2>/dev/null; then
      _update_rollback_hard_stop "could not restore the pre-update PVE host helper from ${UPDATE_HELPER_HOST_PATH}.rollback-${UPDATE_RUN_ID} -- restore it manually before retrying"
    fi
  fi

  if ledger_has update-unit-activated "${VMID}"; then
    if ! pct exec "${VMID}" -- mv "/etc/systemd/system/hubinet-ops.service.rollback-${UPDATE_RUN_ID}" /etc/systemd/system/hubinet-ops.service >/dev/null 2>&1; then
      _update_rollback_hard_stop "could not restore the pre-update systemd unit inside container ${VMID}"
    fi
    pct exec "${VMID}" -- systemctl daemon-reload >/dev/null 2>&1 || true
  fi

  if ledger_has update-venv-activated "${VMID}"; then
    pct exec "${VMID}" -- rm -rf /opt/hubinet-ops/.venv >/dev/null 2>&1 || true
    if ! pct exec "${VMID}" -- mv "/opt/hubinet-ops/.venv.rollback-${UPDATE_RUN_ID}" /opt/hubinet-ops/.venv >/dev/null 2>&1; then
      _update_rollback_hard_stop "could not restore the pre-update virtualenv inside container ${VMID}"
    fi
    if ! pct exec "${VMID}" -- mv "/opt/hubinet-ops/requirements.txt.rollback-${UPDATE_RUN_ID}" /opt/hubinet-ops/requirements.txt >/dev/null 2>&1; then
      _update_rollback_hard_stop "could not restore the pre-update requirements.txt inside container ${VMID}"
    fi
  fi

  if ledger_has update-app-activated "${VMID}"; then
    pct exec "${VMID}" -- rm -rf /opt/hubinet-ops/app >/dev/null 2>&1 || true
    if ! pct exec "${VMID}" -- mv "/opt/hubinet-ops/app.rollback-${UPDATE_RUN_ID}" /opt/hubinet-ops/app >/dev/null 2>&1; then
      _update_rollback_hard_stop "could not restore the pre-update application payload inside container ${VMID}"
    fi
  fi

  run_logged pct exec "${VMID}" -- systemctl start hubinet-ops \
    || _update_rollback_hard_stop "restored the pre-update installation's files, but could not start hubinet-ops inside container ${VMID}"

  local waited=0 state=""
  while (( waited < BOOTSTRAP_SERVICE_TIMEOUT_SECONDS )); do
    state="$(pct exec "${VMID}" -- systemctl is-active hubinet-ops 2>/dev/null || true)"
    [[ "${state}" == "active" ]] && break
    sleep 1
    waited=$(( waited + 1 ))
  done
  [[ "${state}" == "active" ]] \
    || _update_rollback_hard_stop "restored the pre-update installation's files, but it did not become active within ${BOOTSTRAP_SERVICE_TIMEOUT_SECONDS}s (last state: ${state:-unknown})"

  local health_body
  health_body="$(pct exec "${VMID}" -- curl -fsS "http://127.0.0.1:8787/r0/v1/health" 2>/dev/null || true)"
  [[ -n "${health_body}" ]] \
    || _update_rollback_hard_stop "restored the pre-update installation and it reports active, but the unauthenticated health probe returned nothing"

  update_stage_cleanup
  log_warn "rollback complete -- the pre-update installation is running again (exit ${exit_code})"
}

_update_rollback_hard_stop() {
  log_warn "ROLLBACK COULD NOT BE COMPLETED: $*"
  log_warn "Preserving every rollback/backup artifact for manual recovery. Run ${UPDATE_RUN_ID} left the installation in a non-coherent state -- do not assume the service is safe to use until this is resolved by hand."
  exit 1
}
