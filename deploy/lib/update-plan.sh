#!/usr/bin/env bash
# Phase U2 -- classify every replaceable artifact against the exact
# approved target commit, then print one exact plan and obtain approval
# before any managed-state mutation. Nothing in this file mutates managed
# installation state; ephemeral /tmp planning files are cleaned on exit
# (see update-proxmox-0.5.sh's own exit trap).

UPDATE_TOOL_CT_PATH=""
UPDATE_PROBE_CT_PATH=""

UPDATE_INSTALLED_SHA=""
UPDATE_REQUIREMENTS_CHANGED="0"
UPDATE_UNIT_CHANGED="0"
UPDATE_HELPER_CHANGED="0"
UPDATE_TARGET_SCHEMA_MARKER=""
UPDATE_TARGET_SCHEMA_VERSION=""
UPDATE_TARGET_SCHEMA_OBJECTS=""
UPDATE_CURRENT_SCHEMA_MARKER=""
UPDATE_CURRENT_SCHEMA_VERSION=""
UPDATE_CURRENT_BACKEND_INSTANCE_ID=""
UPDATE_AUTHORITY_ACTION=""
UPDATE_PRE_BACKEND_INSTANCE_ID=""
UPDATE_PRE_COMMITTED_SEQUENCE=""
UPDATE_HA_REENROLL_REQUIRED="0"

UPDATE_PLAN_FENCE_REQUIREMENTS_TMP=""
UPDATE_PLAN_FENCE_UNIT_TMP=""
UPDATE_PLAN_FENCE_HELPER_TMP=""
UPDATE_PLAN_FENCE_SCALAR=""

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
  # Run-owned (UPDATE_RUN_ID, generated once by update-proxmox-0.5.sh
  # before Phase U1) rather than a fixed shared /tmp name -- see
  # _update_cleanup_plan_tools below for the matching cleanup on every
  # exit path (dry-run success, ordinary success, pre-stop failure, and
  # the tail of a full rollback).
  [[ -n "${UPDATE_RUN_ID}" ]] || die "internal error: UPDATE_RUN_ID was not set before update_plan_push_tools"
  UPDATE_TOOL_CT_PATH="/tmp/hubinet-ops-authority-tool-${UPDATE_RUN_ID}.py"
  UPDATE_PROBE_CT_PATH="/tmp/hubinet-ops-update-probe-${UPDATE_RUN_ID}.py"
  run_logged pct push "${VMID}" "${UPDATE_SCRIPT_DIR}/hubinet-ops-authority-tool.py" "${UPDATE_TOOL_CT_PATH}" \
    || die "failed to push the authority inspection tool into container ${VMID}"
  run_logged pct push "${VMID}" "${UPDATE_SCRIPT_DIR}/hubinet-ops-update-probe.py" "${UPDATE_PROBE_CT_PATH}" \
    || die "failed to push the pre-update probe into container ${VMID}"
}

# _update_cleanup_plan_tools: best-effort removal of the Phase U2 planning
# tools pushed above. Called on every exit path -- dry-run success,
# ordinary success, and pre-service-stop failure (see
# update-proxmox-0.5.sh and update-stage.sh::update_stage_cleanup) -- and
# also at the tail of a full post-stop rollback, once the authority-tool
# is no longer needed for the (possible) authority-database restore. Not a
# durable journal; nothing here is managed state a future update depends
# on.
_update_cleanup_plan_tools() {
  [[ -n "${VMID:-}" ]] || return 0
  [[ -n "${UPDATE_TOOL_CT_PATH}" ]] && pct exec "${VMID}" -- rm -f "${UPDATE_TOOL_CT_PATH}" >/dev/null 2>&1
  [[ -n "${UPDATE_PROBE_CT_PATH}" ]] && pct exec "${VMID}" -- rm -f "${UPDATE_PROBE_CT_PATH}" >/dev/null 2>&1
  return 0
}

_update_target_file_text() {
  local relative_path="$1"
  git -C "${SOURCE_DIR}" show "${SOURCE_HEAD_SHA}:${relative_path}" 2>/dev/null
}

# --- P2 (correction pass 2): byte-exact classification ---------------------
#
# The "changed" classification above (still used for advisory/schema-
# constant extraction, where it is fine) is NOT safe for the exact-content
# comparisons below: bash command substitution `$(...)` silently strips
# every trailing newline byte from captured output, so two files differing
# ONLY in trailing-newline bytes (e.g. installed "foo\n" vs. target
# "foo\n\n") would compare equal and be misclassified "unchanged" --
# contrary to this updater's exact-content/exact-approved-commit contract.
# These helpers instead redirect the EXACT bytes straight to a file (a
# redirect never strips anything, unlike a substitution) and compare with
# `cmp`, never a string comparison of substitution-captured content.

_update_target_file_to_file() {
  local relative_path="$1" dest_path="$2"
  git -C "${SOURCE_DIR}" show "${SOURCE_HEAD_SHA}:${relative_path}" >"${dest_path}" 2>/dev/null
}

_update_installed_ct_file_to_file() {
  local path="$1" dest_path="$2"
  # `|| : >"${dest_path}"` deliberately swallows a missing/unreadable
  # installed file into "empty" rather than letting this statement's own
  # non-zero exit trip `set -e` -- an absent installed file must classify
  # as "changed" (compared against a non-empty target), never crash the
  # updater outright.
  pct exec "${VMID}" -- cat "${path}" >"${dest_path}" 2>/dev/null || : >"${dest_path}"
}

# _update_files_differ_exact: true (exit 0) only if the two files are
# positively known to differ byte-for-byte; false (exit 1) only if they
# are positively known equal. Any other cmp outcome is a planning error,
# never a normal "changed" classification.
_update_files_differ_exact() {
  local rc
  if cmp -s "$1" "$2"; then
    return 1
  else
    rc=$?
  fi
  if (( rc == 1 )); then
    return 0
  fi
  die "exact comparison failed for planning inputs '$1' and '$2' (cmp exit ${rc}) -- refusing to classify an artifact from an unknown comparison result"
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
  local installed_tmp target_tmp
  installed_tmp="$(secret_tmpfile "/tmp/hubinet-ops-update-classify.XXXXXX")"
  target_tmp="$(secret_tmpfile "/tmp/hubinet-ops-update-classify.XXXXXX")"
  _update_installed_ct_file_to_file /opt/hubinet-ops/requirements.txt "${installed_tmp}"
  _update_target_file_to_file requirements.txt "${target_tmp}" \
    || die "target commit ${SOURCE_HEAD_SHA} has no requirements.txt -- refusing to plan an update against an unreadable target"
  if _update_files_differ_exact "${installed_tmp}" "${target_tmp}"; then
    UPDATE_REQUIREMENTS_CHANGED="1"
  fi
  # The exact installed bytes used above are also the immutable
  # invocation-local plan-fence baseline. Never re-read the live file to
  # manufacture a second baseline after the operator has seen the plan.
  UPDATE_PLAN_FENCE_REQUIREMENTS_TMP="${installed_tmp}"
}

_update_classify_unit() {
  local installed_tmp target_tmp
  installed_tmp="$(secret_tmpfile "/tmp/hubinet-ops-update-classify.XXXXXX")"
  target_tmp="$(secret_tmpfile "/tmp/hubinet-ops-update-classify.XXXXXX")"
  _update_installed_ct_file_to_file /etc/systemd/system/hubinet-ops.service "${installed_tmp}"
  _update_target_file_to_file deploy/hubinet-ops-0.5.service "${target_tmp}" \
    || die "target commit ${SOURCE_HEAD_SHA} has no deploy/hubinet-ops-0.5.service -- refusing to plan an update against an unreadable target"
  if _update_files_differ_exact "${installed_tmp}" "${target_tmp}"; then
    UPDATE_UNIT_CHANGED="1"
  fi
  UPDATE_PLAN_FENCE_UNIT_TMP="${installed_tmp}"
}

_update_classify_helper() {
  local helper_host_path installed_tmp target_tmp
  helper_host_path="$(_host_control_host_path "${UPDATE_HELPER_PATH}")"
  installed_tmp="$(secret_tmpfile "/tmp/hubinet-ops-update-classify.XXXXXX")"
  target_tmp="$(secret_tmpfile "/tmp/hubinet-ops-update-classify.XXXXXX")"
  cat "${helper_host_path}" >"${installed_tmp}" 2>/dev/null || : >"${installed_tmp}"
  _update_target_file_to_file deploy/hubinet-package-scan-helper.py "${target_tmp}" \
    || die "target commit ${SOURCE_HEAD_SHA} has no deploy/hubinet-package-scan-helper.py -- refusing to plan an update against an unreadable target"
  if _update_files_differ_exact "${installed_tmp}" "${target_tmp}"; then
    UPDATE_HELPER_CHANGED="1"
  fi
  UPDATE_PLAN_FENCE_HELPER_TMP="${installed_tmp}"
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

  # P2-C: static (non-executing) extraction of the target's REQUIRED
  # authority schema-object contract -- every table/index/trigger name
  # folded into app/inventory/store.py's _REQUIRED_SCHEMA_OBJECTS
  # (_REQUIRED_TABLES unioned with the extra index/trigger names) -- a
  # lexical scan of the quoted identifiers between the _REQUIRED_TABLES
  # definition and the following _LEGACY_TABLES definition, never an
  # import or execution of target application code (AGENTS.md task
  # prompt section 11.E). Used only to preflight-validate a would-be
  # SCHEMA-PRESERVING update (_update_verify_preserve_schema_objects,
  # below) before the service is ever stopped.
  UPDATE_TARGET_SCHEMA_OBJECTS="$(printf '%s\n' "${target_text}" | python3 -c '
import re, sys
text = sys.stdin.read()
start = text.find("_REQUIRED_TABLES")
end = text.find("_LEGACY_TABLES")
names = set()
if start != -1 and end != -1 and end > start:
    names = set(re.findall("\"([A-Za-z0-9_]+)\"", text[start:end]))
print(" ".join(sorted(names)))
')"
  [[ -n "${UPDATE_TARGET_SCHEMA_OBJECTS}" ]] \
    || die "could not statically determine the required authority schema-object set from target commit ${SOURCE_HEAD_SHA}'s app/inventory/store.py"
}

# _update_preserve_schema_objects_match /
# _update_verify_preserve_schema_objects (P2-C): a matching marker/
# version/backend-identity classification alone is weaker than the
# target runtime's own schema validation (app/inventory/store.py's
# _open_or_initialize checks the exact _REQUIRED_SCHEMA_OBJECTS set, not
# only marker/version/user_version/backend identity) -- an installed DB
# that this updater would otherwise classify "preserve" could still be
# REJECTED by the target runtime at restart if it has drifted
# structurally (a missing table/index/trigger) while keeping a coherent
# marker/version. This proves the live DB's actual schema_objects
# (reported by hubinet-ops-authority-tool.py's inspect, above) match the
# target's statically-extracted required set -- BEFORE the service is
# ever stopped. A mismatch here is not an authority reset (no version
# transition exists to justify one); it fails closed instead.
_update_preserve_schema_objects_match() {
  local inspect_output="$1"
  python3 -c '
import json, sys
data = json.loads(sys.argv[1])
actual = set(data.get("schema_objects") or [])
expected = set(sys.argv[2].split())
sys.exit(0 if actual == expected else 1)
' "${inspect_output}" "${UPDATE_TARGET_SCHEMA_OBJECTS}"
}

_update_verify_preserve_schema_objects() {
  local inspect_output="$1" context="${2:-classification}"
  if _update_preserve_schema_objects_match "${inspect_output}"; then
    return 0
  fi
  if [[ "${context}" == "plan_fence" ]]; then
    die "immediately-before-mutation plan fence failed: the authority database's required schema objects (tables/indexes/triggers) changed since the approved preserve plan was classified -- refusing to mutate or silently switch to an authority reset; investigate the structural drift and rerun planning/approval"
  fi
  die "the current authority database's marker/schema_version look schema-preserving-compatible (marker=${UPDATE_CURRENT_SCHEMA_MARKER}, version=${UPDATE_CURRENT_SCHEMA_VERSION}), but its actual schema objects (tables/indexes/triggers) do not match target commit ${SOURCE_HEAD_SHA}'s required set for that version -- refusing a schema-preserving update against a structurally drifted database (this is not an authority reset; no version transition exists for this classification). Investigate and repair the database manually before retrying."
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
    _update_verify_preserve_schema_objects "${inspect_output}"
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
  # The ACTIVE WORKLOAD UPDATE JOB FENCE. This runs in Phase U2 --
  # classification -- which is strictly before staging, before the service
  # is stopped, and before any helper, key, config file, or systemd unit is
  # touched. Once workload mutation is live, replacing the backend or its
  # privileged helpers while a job owns a snapshot, mutation, or rollback
  # journal can pair a new backend with a half-replaced helper set for an
  # operation already in flight, so this refuses rather than negotiating.
  #
  # There is deliberately NO bypass flag. An operator whose update is
  # genuinely stuck resolves the job through the product's own explicit
  # controls (resume, or rollback) and then runs the updater again.
  local update_active update_job_id update_checkpoint
  update_active="$(_json_field_from_text "${probe_output}" "package_update_active")"
  if [[ "${update_active}" == "1" ]]; then
    update_job_id="$(_json_field_from_text "${probe_output}" "package_update_job_id")"
    update_checkpoint="$(_json_field_from_text "${probe_output}" "package_update_checkpoint")"
    die "refusing to update: package update job ${update_job_id:-unknown} is ACTIVE at checkpoint ${update_checkpoint:-unknown} on this installation. Nothing has been changed. Let it finish, or resolve it through the operator controls (resume or roll back), then run this updater again."
  fi
  # An empty value is the pre-activation backend whose /package-update/active
  # route does not exist. That backend has no update worker and no way to
  # own a workload job, so there is nothing to fence -- and the probe only
  # reports it for a real HTTP 404, never for an unreachable backend.

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
  update_boundaries_classify
  _update_classify_authority
  _update_pre_probe
  _update_capture_plan_fence_from_classification

  log_pass "classification complete"
}

update_plan_print() {
  local requirements_plan
  if [[ "${UPDATE_REQUIREMENTS_CHANGED}" == "1" ]]; then
    requirements_plan="changed -- service is stopped; current virtualenv is preserved for rollback; target virtualenv is built at the FINAL live path /opt/hubinet-ops/.venv; pip installs the exact target requirements DURING the maintenance window (which can extend downtime); activation failure restores the prior environment"
  else
    requirements_plan="unchanged -- existing virtualenv is preserved; no virtualenv rebuild or pip/dependency installation occurs"
  fi

  cat <<PLAN

Hubinet Ops in-place update plan
=================================
VMID:                          ${VMID}
Installed source commit:       ${UPDATE_INSTALLED_SHA}
Target source commit:          ${SOURCE_HEAD_SHA}
backend_instance_id (before):  ${UPDATE_PRE_BACKEND_INSTANCE_ID}
Application payload:           replace (tracked files at target commit)
requirements.txt:              ${requirements_plan}
systemd unit:                  $( [[ "${UPDATE_UNIT_CHANGED}" == "1" ]] && printf 'changed -- will be replaced during activation' || printf 'unchanged -- left in place' )
PVE host helper:               $( [[ "${UPDATE_HELPER_CHANGED}" == "1" ]] && printf 'changed -- content will be replaced at the SAME path (%s)' "${UPDATE_HELPER_PATH}" || printf 'unchanged -- left in place' )
Package-update boundaries:     $(update_boundaries_plan_summary)
Active workload update job:    none (verified before this plan was printed -- an active job refuses this updater outright)
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

# --- Immediately-before-mutation plan fence (correction pass 9, P1, ------
# section 11) -----------------------------------------------------------
#
# The per-VMID updater flock and the ownership fence
# (update_ownership_verify's "revalidate" mode) together close the
# "different installation entirely" TOCTOU class between planning and
# mutation. They do NOT close a narrower one: a PVE snapshot/restore can
# legitimately preserve the installation run-id while rolling its LIVE
# software/database state backward. This bounded, in-memory plan
# fingerprint closes that gap for exactly the facts that define THIS
# approved plan. Its file baselines ARE the exact installed-byte files
# already used by classification, and its scalar baseline is finalized
# from the original ownership/classification values before the plan is
# displayed. Live state is re-read and compared immediately before the
# first managed-state mutation.
#
# Deliberately excludes every naturally-changing runtime fact (discovery
# sequence, observed timestamps, ordinary authority DB contents,
# package-scan rows): an ordinary background discovery cycle occurring
# while the operator reads the plan must never invalidate it. This is one
# bounded fence for this invocation, not a generic CAS framework or new
# durable state.

# _update_capture_plan_fence_from_classification: called exactly once at
# the end of update_plan_classify, after every scalar classification fact
# has been established and before the plan is displayed. The three file
# baselines were assigned directly by their classification reads above;
# this function must never read live installation state.
_update_capture_plan_fence_from_classification() {
  [[ -n "${UPDATE_PLAN_FENCE_REQUIREMENTS_TMP}" \
     && -n "${UPDATE_PLAN_FENCE_UNIT_TMP}" \
     && -n "${UPDATE_PLAN_FENCE_HELPER_TMP}" ]] \
    || die "internal error: classification did not retain every file-based plan-fence baseline"

  UPDATE_PLAN_FENCE_SCALAR="run_id=${UPDATE_INSTALLATION_RUN_ID} authority_action=${UPDATE_AUTHORITY_ACTION} authority_marker=${UPDATE_CURRENT_SCHEMA_MARKER} authority_schema_version=${UPDATE_CURRENT_SCHEMA_VERSION} authority_backend_instance_id=${UPDATE_CURRENT_BACKEND_INSTANCE_ID} live_backend_instance_id=${UPDATE_PRE_BACKEND_INSTANCE_ID}"
}

# _update_revalidate_plan_fence: re-reads the SAME bounded facts
# immediately before the first mutation and requires them to still match
# exactly -- byte-exact for the three file-based facts (via
# _update_files_differ_exact, never a `$(...)`-stripped-newline string
# compare), plain string equality for the scalar facts. Any mismatch fails
# BEFORE any managed-state mutation, telling the operator to rerun
# planning/approval. UPDATE_TOOL_CT_PATH/UPDATE_PROBE_CT_PATH are already
# present in the container from Phase U2 (update_plan_push_tools); this
# never re-pushes or re-plans anything else.
_update_revalidate_plan_fence() {
  [[ -n "${UPDATE_PLAN_FENCE_REQUIREMENTS_TMP}" ]] \
    || die "internal error: the plan fence was never captured before revalidation"

  local fresh_requirements_tmp fresh_unit_tmp fresh_helper_tmp
  fresh_requirements_tmp="$(secret_tmpfile "/tmp/hubinet-ops-update-planfence.XXXXXX")"
  fresh_unit_tmp="$(secret_tmpfile "/tmp/hubinet-ops-update-planfence.XXXXXX")"
  fresh_helper_tmp="$(secret_tmpfile "/tmp/hubinet-ops-update-planfence.XXXXXX")"
  _update_installed_ct_file_to_file /opt/hubinet-ops/requirements.txt "${fresh_requirements_tmp}"
  _update_installed_ct_file_to_file /etc/systemd/system/hubinet-ops.service "${fresh_unit_tmp}"
  local helper_host_path
  helper_host_path="$(_host_control_host_path "${UPDATE_HELPER_PATH}")"
  cat "${helper_host_path}" >"${fresh_helper_tmp}" 2>/dev/null || : >"${fresh_helper_tmp}"

  if _update_files_differ_exact "${UPDATE_PLAN_FENCE_REQUIREMENTS_TMP}" "${fresh_requirements_tmp}"; then
    die "immediately-before-mutation plan fence failed: the installed requirements.txt changed since the approved plan was classified -- refusing to mutate; rerun planning/approval"
  fi
  if _update_files_differ_exact "${UPDATE_PLAN_FENCE_UNIT_TMP}" "${fresh_unit_tmp}"; then
    die "immediately-before-mutation plan fence failed: the installed systemd unit changed since the approved plan was classified -- refusing to mutate; rerun planning/approval"
  fi
  if _update_files_differ_exact "${UPDATE_PLAN_FENCE_HELPER_TMP}" "${fresh_helper_tmp}"; then
    die "immediately-before-mutation plan fence failed: the installed PVE host helper changed since the approved plan was classified -- refusing to mutate; rerun planning/approval"
  fi

  local inspect_output inspect_status fresh_marker fresh_version fresh_authority_backend_instance_id
  inspect_output="$(pct exec "${VMID}" -- python3 "${UPDATE_TOOL_CT_PATH}" inspect /var/lib/hubinet-ops/authority.db 2>/dev/null)" \
    && inspect_status=0 || inspect_status=$?
  (( inspect_status == 0 )) && _json_bool_field_is_true "${inspect_output}" "ok" \
    || die "immediately-before-mutation plan fence failed: could not re-read the authority database's identity -- refusing to mutate; rerun planning/approval"
  fresh_marker="$(_json_field_from_text "${inspect_output}" "marker")"
  fresh_version="$(_json_field_from_text "${inspect_output}" "schema_version")"
  fresh_authority_backend_instance_id="$(_json_field_from_text "${inspect_output}" "backend_instance_id")"

  # Preserve classification depends on the exact required table/index/
  # trigger set, not only marker/version/backend identity. Re-run that
  # same proof from this fresh inspect immediately before mutation. A
  # reset-required plan deliberately does not require the old schema to
  # have the target's object set: its explicitly approved action replaces
  # the old authority database after taking a validated backup.
  if [[ "${UPDATE_AUTHORITY_ACTION}" == "preserve" ]]; then
    _update_verify_preserve_schema_objects "${inspect_output}" plan_fence
  fi

  local probe_output probe_status fresh_backend_instance_id
  probe_output="$(pct exec "${VMID}" -- python3 "${UPDATE_PROBE_CT_PATH}" 2>/dev/null)" \
    && probe_status=0 || probe_status=$?
  (( probe_status == 0 )) && _json_bool_field_is_true "${probe_output}" "ok" \
    || die "immediately-before-mutation plan fence failed: could not re-read the live backend identity -- refusing to mutate; rerun planning/approval"
  fresh_backend_instance_id="$(_json_field_from_text "${probe_output}" "backend_instance_id")"

  local fresh_authority_action
  if [[ "${fresh_marker}" == "${UPDATE_TARGET_SCHEMA_MARKER}" \
        && "${fresh_version}" == "${UPDATE_TARGET_SCHEMA_VERSION}" ]]; then
    fresh_authority_action="preserve"
  else
    fresh_authority_action="reset_required"
  fi

  local fresh_scalar="run_id=${UPDATE_VMID_RUN_ID} authority_action=${fresh_authority_action} authority_marker=${fresh_marker} authority_schema_version=${fresh_version} authority_backend_instance_id=${fresh_authority_backend_instance_id} live_backend_instance_id=${fresh_backend_instance_id}"
  [[ "${fresh_scalar}" == "${UPDATE_PLAN_FENCE_SCALAR}" ]] \
    || die "immediately-before-mutation plan fence failed: the authority schema identity or backend instance changed since the approved plan was classified (was: ${UPDATE_PLAN_FENCE_SCALAR}; now: ${fresh_scalar}) -- refusing to mutate; rerun planning/approval"
}
