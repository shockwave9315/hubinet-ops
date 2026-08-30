#!/usr/bin/env bash
# Phase U2 -- classify every replaceable artifact against the exact
# approved target commit, then print one exact plan and obtain approval
# before any managed-state mutation. Nothing in this file mutates managed
# installation state; ephemeral /tmp planning files are cleaned on exit
# (see update-proxmox-0.5.sh's own exit trap).

UPDATE_TOOL_CT_PATH="/tmp/hubinet-ops-authority-tool.py"
UPDATE_PROBE_CT_PATH="/tmp/hubinet-ops-update-probe.py"

UPDATE_INSTALLED_SHA=""
UPDATE_REQUIREMENTS_CHANGED="0"
UPDATE_UNIT_CHANGED="0"
UPDATE_HELPER_CHANGED="0"
UPDATE_TARGET_SCHEMA_MARKER=""
UPDATE_TARGET_SCHEMA_VERSION=""
UPDATE_CURRENT_SCHEMA_MARKER=""
UPDATE_CURRENT_SCHEMA_VERSION=""
UPDATE_CURRENT_BACKEND_INSTANCE_ID=""
UPDATE_AUTHORITY_ACTION=""
UPDATE_PRE_BACKEND_INSTANCE_ID=""
UPDATE_PRE_COMMITTED_SEQUENCE=""
UPDATE_HA_REENROLL_REQUIRED="0"

# _json_field_from_text <json-text> <key>: prints the string/int/bool
# value (as text) of a top-level key from a small, already-trusted JSON
# object this repository's own helper scripts produced -- never operator-
# supplied or attacker-controlled text. python3 is a hard preflight
# requirement, exactly like bootstrap-common.sh's own JSON helpers.
_json_field_from_text() {
  local text="$1" key="$2"
  python3 -c '
import json, sys
data = json.loads(sys.argv[2])
value = data.get(sys.argv[1])
if value is None:
    print("")
elif isinstance(value, bool):
    print("1" if value else "0")
else:
    print(value)
' "${key}" "${text}" 2>/dev/null
}

_json_bool_field_is_true() {
  local text="$1" key="$2"
  python3 -c '
import json, sys
data = json.loads(sys.argv[2])
sys.exit(0 if data.get(sys.argv[1]) is True else 1)
' "${key}" "${text}" 2>/dev/null
}

update_plan_push_tools() {
  run_logged pct push "${VMID}" "${UPDATE_SCRIPT_DIR}/hubinet-ops-authority-tool.py" "${UPDATE_TOOL_CT_PATH}" \
    || die "failed to push the authority inspection tool into container ${VMID}"
  run_logged pct push "${VMID}" "${UPDATE_SCRIPT_DIR}/hubinet-ops-update-probe.py" "${UPDATE_PROBE_CT_PATH}" \
    || die "failed to push the pre-update probe into container ${VMID}"
}

_update_target_file_text() {
  local relative_path="$1"
  git -C "${SOURCE_DIR}" show "${SOURCE_HEAD_SHA}:${relative_path}" 2>/dev/null
}

_update_installed_ct_file_text() {
  local path="$1"
  pct exec "${VMID}" -- cat "${path}" 2>/dev/null
}

_update_read_installed_source_sha() {
  local raw status
  raw="$(pct exec "${VMID}" -- cat /opt/hubinet-ops/.hubinet-source-commit 2>/dev/null)" && status=0 || status=$?
  if (( status != 0 )) || [[ -z "${raw}" ]]; then
    UPDATE_INSTALLED_SHA="unknown (pre-updater install)"
    return 0
  fi
  raw="$(printf '%s' "${raw}" | tr -d '[:space:]')"
  if [[ "${raw}" =~ ^[0-9a-f]{40}$ ]]; then
    UPDATE_INSTALLED_SHA="${raw}"
  else
    log_warn "installed source marker (/opt/hubinet-ops/.hubinet-source-commit) is malformed -- treating installed source as unknown"
    UPDATE_INSTALLED_SHA="unknown (pre-updater install)"
  fi
}

_update_classify_requirements() {
  local installed target
  installed="$(_update_installed_ct_file_text /opt/hubinet-ops/requirements.txt)"
  target="$(_update_target_file_text requirements.txt)"
  [[ -n "${target}" ]] || die "target commit ${SOURCE_HEAD_SHA} has no requirements.txt -- refusing to plan an update against an unreadable target"
  if [[ "${installed}" != "${target}" ]]; then
    UPDATE_REQUIREMENTS_CHANGED="1"
  fi
}

_update_classify_unit() {
  local installed target
  installed="$(_update_installed_ct_file_text /etc/systemd/system/hubinet-ops.service)"
  target="$(_update_target_file_text deploy/hubinet-ops-0.5.service)"
  [[ -n "${target}" ]] || die "target commit ${SOURCE_HEAD_SHA} has no deploy/hubinet-ops-0.5.service -- refusing to plan an update against an unreadable target"
  if [[ "${installed}" != "${target}" ]]; then
    UPDATE_UNIT_CHANGED="1"
  fi
}

_update_classify_helper() {
  local helper_host_path installed target
  helper_host_path="$(_host_control_host_path "${UPDATE_HELPER_PATH}")"
  installed="$(cat "${helper_host_path}" 2>/dev/null)"
  target="$(_update_target_file_text deploy/hubinet-package-scan-helper.py)"
  [[ -n "${target}" ]] || die "target commit ${SOURCE_HEAD_SHA} has no deploy/hubinet-package-scan-helper.py -- refusing to plan an update against an unreadable target"
  if [[ "${installed}" != "${target}" ]]; then
    UPDATE_HELPER_CHANGED="1"
  fi
}

# _update_target_authority_schema: a static, non-executing read of the
# two fixed constants app/inventory/store.py declares at module scope --
# never imports or executes any target application code (AGENTS.md task
# prompt section 11.E: "without executing arbitrary target application
# imports if a static/AST read of the constant is sufficient").
_update_target_authority_schema() {
  local target_text
  target_text="$(_update_target_file_text app/inventory/store.py)"
  [[ -n "${target_text}" ]] \
    || die "target commit ${SOURCE_HEAD_SHA} has no app/inventory/store.py -- cannot determine the target authority schema"
  UPDATE_TARGET_SCHEMA_MARKER="$(printf '%s\n' "${target_text}" | python3 -c '
import re, sys
text = sys.stdin.read()
match = re.search(r"^AUTHORITY_SCHEMA_MARKER\s*=\s*\"([^\"]+)\"", text, re.MULTILINE)
print(match.group(1) if match else "")
')"
  UPDATE_TARGET_SCHEMA_VERSION="$(printf '%s\n' "${target_text}" | python3 -c '
import re, sys
text = sys.stdin.read()
match = re.search(r"^AUTHORITY_SCHEMA_VERSION\s*=\s*(\d+)", text, re.MULTILINE)
print(match.group(1) if match else "")
')"
  [[ -n "${UPDATE_TARGET_SCHEMA_MARKER}" && "${UPDATE_TARGET_SCHEMA_VERSION}" =~ ^[0-9]+$ ]] \
    || die "could not statically determine AUTHORITY_SCHEMA_MARKER/AUTHORITY_SCHEMA_VERSION from target commit ${SOURCE_HEAD_SHA}'s app/inventory/store.py"
}

_update_classify_authority() {
  local inspect_output status
  inspect_output="$(pct exec "${VMID}" -- python3 "${UPDATE_TOOL_CT_PATH}" inspect /var/lib/hubinet-ops/authority.db 2>/dev/null)" && status=0 || status=$?
  (( status == 0 )) && [[ -n "${inspect_output}" ]] \
    || die "could not run the authority inspection tool inside container ${VMID} -- refusing to plan an update without a reliable read of the current authority database"

  if ! _json_bool_field_is_true "${inspect_output}" "ok"; then
    local reason
    reason="$(_json_field_from_text "${inspect_output}" "reason")"
    die "authority database inspection failed (${reason:-unknown}) -- this looks like an allegedly installed, active product with a missing, empty, or unrecognized authority database. This updater fails closed rather than silently treating that abnormal condition as an authority reset; investigate manually before retrying."
  fi

  UPDATE_CURRENT_SCHEMA_MARKER="$(_json_field_from_text "${inspect_output}" "marker")"
  UPDATE_CURRENT_SCHEMA_VERSION="$(_json_field_from_text "${inspect_output}" "schema_version")"
  UPDATE_CURRENT_BACKEND_INSTANCE_ID="$(_json_field_from_text "${inspect_output}" "backend_instance_id")"

  _update_target_authority_schema

  if [[ "${UPDATE_CURRENT_SCHEMA_MARKER}" == "${UPDATE_TARGET_SCHEMA_MARKER}" \
        && "${UPDATE_CURRENT_SCHEMA_VERSION}" == "${UPDATE_TARGET_SCHEMA_VERSION}" ]]; then
    UPDATE_AUTHORITY_ACTION="preserve"
    UPDATE_HA_REENROLL_REQUIRED="0"
  else
    UPDATE_AUTHORITY_ACTION="reset_required"
    UPDATE_HA_REENROLL_REQUIRED="1"
  fi
}

_update_pre_probe() {
  local probe_output status
  probe_output="$(pct exec "${VMID}" -- python3 "${UPDATE_PROBE_CT_PATH}" 2>/dev/null)" && status=0 || status=$?
  (( status == 0 )) && [[ -n "${probe_output}" ]] \
    || die "could not run the pre-update probe inside container ${VMID}"
  if ! _json_bool_field_is_true "${probe_output}" "ok"; then
    local reason
    reason="$(_json_field_from_text "${probe_output}" "reason")"
    die "pre-update live probe failed (${reason:-unknown}) -- refusing to update a product that does not currently prove it is live and reachable"
  fi
  UPDATE_PRE_BACKEND_INSTANCE_ID="$(_json_field_from_text "${probe_output}" "backend_instance_id")"
  UPDATE_PRE_COMMITTED_SEQUENCE="$(_json_field_from_text "${probe_output}" "last_committed_run_sequence")"
  [[ -n "${UPDATE_PRE_BACKEND_INSTANCE_ID}" ]] \
    || die "pre-update live probe returned no backend_instance_id"

  local systemd_active enabled
  systemd_active="$(pct exec "${VMID}" -- systemctl is-active hubinet-ops 2>/dev/null || true)"
  [[ "${systemd_active}" == "active" ]] \
    || die "hubinet-ops is not active inside container ${VMID} (${systemd_active:-unknown}) -- this updater requires a currently-running installation"
  enabled="$(pct exec "${VMID}" -- systemctl is-enabled hubinet-ops 2>/dev/null || true)"
  [[ "${enabled}" == "enabled" ]] \
    || die "hubinet-ops is not enabled inside container ${VMID} (${enabled:-unknown})"
}

update_plan_classify() {
  log_phase "Phase U2: classify target artifacts"

  update_plan_push_tools
  _update_read_installed_source_sha
  _update_classify_requirements
  _update_classify_unit
  _update_classify_helper
  _update_classify_authority
  _update_pre_probe

  log_pass "classification complete"
}

update_plan_print() {
  cat <<PLAN

Hubinet Ops in-place update plan
=================================
VMID:                          ${VMID}
Installed source commit:       ${UPDATE_INSTALLED_SHA}
Target source commit:          ${SOURCE_HEAD_SHA}
backend_instance_id (before):  ${UPDATE_PRE_BACKEND_INSTANCE_ID}
Application payload:           replace (tracked files at target commit)
requirements.txt:              $( [[ "${UPDATE_REQUIREMENTS_CHANGED}" == "1" ]] && printf 'changed -- new venv will be staged and swapped' || printf 'unchanged -- venv untouched, no pip/apt run' )
systemd unit:                  $( [[ "${UPDATE_UNIT_CHANGED}" == "1" ]] && printf 'changed -- will be replaced during activation' || printf 'unchanged -- left in place' )
PVE host helper:               $( [[ "${UPDATE_HELPER_CHANGED}" == "1" ]] && printf 'changed -- content will be replaced at the SAME path (%s)' "${UPDATE_HELPER_PATH}" || printf 'unchanged -- left in place' )
Authority schema:              ${UPDATE_CURRENT_SCHEMA_VERSION} -> ${UPDATE_TARGET_SCHEMA_VERSION}
Authority action:              $( [[ "${UPDATE_AUTHORITY_ACTION}" == "preserve" ]] && printf 'preserve (schema unchanged, database and every stored fact kept as-is)' || printf 'RESET REQUIRED -- no migration exists for this schema transition' )
Home Assistant re-enrollment:  $( [[ "${UPDATE_HA_REENROLL_REQUIRED}" == "1" ]] && printf 'REQUIRED after this update (backend_instance_id will change)' || printf 'not required (backend_instance_id is preserved)' )
PLAN

  if [[ "${UPDATE_AUTHORITY_ACTION}" == "reset_required" ]]; then
    cat <<RESET

DESTRUCTIVE AUTHORITY RESET
----------------------------
The installed authority schema (${UPDATE_CURRENT_SCHEMA_VERSION}) does not match the
target schema (${UPDATE_TARGET_SCHEMA_VERSION}), and no migration exists for this
transition. If you proceed:

  - a coherent backup of the CURRENT authority database will be made
    (inside the CT, under /var/lib/hubinet-ops/update-backups/) and
    validated before anything is removed;
  - the live authority database will then be removed and recreated fresh
    by the TARGET runtime on its first start -- this updater never writes
    schema DDL itself;
  - the LXC is NOT recreated, its VMID/IP/network are NOT changed;
  - the PVE identity/token, PVE token secret, and HA bearer are preserved;
  - inventory.yaml/agent.env, TLS trust material, the host-control key,
    pinned known_hosts, and the nftables firewall are all preserved;

What is LOST from the authority database:
  - backend_instance_id (a new one will be generated);
  - inventory source identity and every node/resource identity;
  - retained missing/replaced history;
  - discovery-run history;
  - package-scan history and every exact stored package row;
  - exact-plan approvals.

Home Assistant re-enrollment WILL be required after this reset, because
backend_instance_id and every resource identity are regenerated -- the
existing config entry will report "wrong_instance" until re-added.
RESET
  fi
}

update_plan_confirm() {
  confirm_or_abort "Proceed with this update plan?"

  if [[ "${UPDATE_AUTHORITY_ACTION}" != "reset_required" ]]; then
    return 0
  fi

  if [[ "${BOOTSTRAP_NON_INTERACTIVE}" == "1" ]]; then
    if [[ "${BOOTSTRAP_ASSUME_YES}" == "1" && "${UPDATE_ALLOW_AUTHORITY_RESET}" == "1" ]]; then
      return 0
    fi
    die "authority reset is required for this update, but --non-interactive was given without BOTH --yes and --allow-authority-reset -- --yes alone never authorizes a destructive authority reset. No managed-state mutation has occurred."
  fi

  local reply
  read -r -p "This update REQUIRES a destructive authority database reset (see above). Type 'reset' to authorize it, anything else to abort: " reply \
    || die "no confirmation could be read from stdin for the destructive authority reset -- aborting. No managed-state mutation has occurred."
  [[ "${reply}" == "reset" ]] \
    || die "destructive authority reset was not explicitly authorized -- aborting. No managed-state mutation has occurred."
}
