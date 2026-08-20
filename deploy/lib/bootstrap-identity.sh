#!/usr/bin/env bash
# Phase 6 -- dedicated read-only PVE identity (user/role/token).
# Phase 7 -- PVE TLS trust material.
#
# Exact shape mirrors docs/operations/0.5-r0-operational-activation.md
# section 2, automated: user hubinetops@pve, role HubinetOpsR0Auditor with
# privileges exactly Sys.Audit,VM.Audit, token r0-readonly with privsep=1,
# ACL granted to BOTH the user and the token at path / with propagate 1.
#
# Fresh-install semantics only (see docs/README section "Idempotency"):
# any pre-existing user/role/token with these exact names is a conflict
# and STOPs the run -- this bootstrap never silently adopts an existing
# identity of unknown provenance.
#
# Secret handling: the raw token secret never enters this script's own
# argv or a bash variable except as bytes redirected directly between two
# files/processes (see PVE_TOKEN_SECRET_FILE below and bootstrap-common.sh
# _json_string_value). It is never passed to jq/python3/awk as a literal
# -a/-v/positional argument.

PVE_USER="hubinetops@pve"
PVE_ROLE="HubinetOpsR0Auditor"
PVE_TOKEN_ID="r0-readonly"
PVE_FULL_TOKEN_ID="${PVE_USER}!${PVE_TOKEN_ID}"
# The exact, complete set this bootstrap ever grants -- not a minimum, not
# a blacklist of what to avoid: verification (see _verify_effective_permissions
# below) asserts the token's effective privilege set equals this set
# exactly, so any unexpected additional privilege (however named) is
# caught, not just the specific mutation privileges named in comments
# elsewhere in this repo's docs.
PVE_REQUIRED_PRIVS="Sys.Audit,VM.Audit"

phase6_pve_identity() {
  log_phase "Phase 6: dedicated read-only PVE identity"

  local user_list_file
  user_list_file="$(mktemp /tmp/hubinet-ops-bootstrap-userlist.XXXXXX.json)"
  chmod 0600 "${user_list_file}"
  pveum user list --output-format json >"${user_list_file}" 2>/dev/null || true
  if _json_list_field_equals "${user_list_file}" "userid" "${PVE_USER}"; then
    rm -f "${user_list_file}"
    die "PVE user '${PVE_USER}' already exists -- this bootstrap only performs a fresh install and will not reuse an existing identity of unknown provenance. Remove it yourself (pveum user delete ${PVE_USER}) after confirming it is not depended on elsewhere, then retry."
  fi
  rm -f "${user_list_file}"

  local role_list_file
  role_list_file="$(mktemp /tmp/hubinet-ops-bootstrap-rolelist.XXXXXX.json)"
  chmod 0600 "${role_list_file}"
  pveum role list --output-format json >"${role_list_file}" 2>/dev/null || true
  if _json_list_field_equals "${role_list_file}" "roleid" "${PVE_ROLE}"; then
    rm -f "${role_list_file}"
    die "PVE role '${PVE_ROLE}' already exists -- refusing to reuse it (unknown provenance/privilege history). Remove it yourself (pveum role delete ${PVE_ROLE}) after confirming it is not depended on elsewhere, then retry."
  fi
  rm -f "${role_list_file}"

  # Recorded only NOW -- after both pre-existing-conflict checks above,
  # immediately before the first real mutation -- never before them. If
  # this marker were recorded earlier, a conflict-refusal die() (which
  # touches nothing) would still make rollback attempt to delete a
  # PRE-EXISTING user/role/token this run never created, violating "never
  # touch a pre-existing resource." From this point on, if a later step
  # mutates PVE state but then reports a nonzero exit for an unrelated
  # reason (a real possibility with `pveum`, not merely local bash control
  # flow), rollback still knows to attempt cleanup of every object this
  # phase might have touched -- see rollback_on_failure in
  # bootstrap-proxmox-0.5.sh, which gates the whole PVE-identity cleanup
  # block on this single marker rather than on each sub-step's own
  # success-only ledger entry.
  ledger_record pve-identity-attempted "${PVE_USER}"

  run_logged pveum user add "${PVE_USER}" --comment "Hubinet Ops 0.5 R0 read-only discovery (created by bootstrap-proxmox-0.5.sh)" \
    || die "failed to create PVE user ${PVE_USER}"
  ledger_record pve-user "${PVE_USER}"

  run_logged pveum role add "${PVE_ROLE}" --privs "${PVE_REQUIRED_PRIVS}" \
    || die "failed to create PVE role ${PVE_ROLE}"
  ledger_record pve-role "${PVE_ROLE}"

  run_logged pveum acl modify / --users "${PVE_USER}" --roles "${PVE_ROLE}" --propagate 1 \
    || die "failed to grant role ${PVE_ROLE} to user ${PVE_USER} at /"
  ledger_record pve-acl-user "${PVE_USER}"

  # This output contains the raw token secret in its "value" field -- use
  # secret_tmpfile (restrictive mode, tracked for guaranteed cleanup on
  # ANY exit path, including a die() below) rather than an ad hoc path,
  # so a parse failure can never leave the secret behind unregistered.
  local token_create_output
  token_create_output="$(secret_tmpfile "/tmp/hubinet-ops-bootstrap-token-create.XXXXXX.json")"
  run_logged pveum user token add "${PVE_USER}" "${PVE_TOKEN_ID}" --privsep 1 \
    --comment "R0 read-only discovery token (created by bootstrap-proxmox-0.5.sh)" \
    --output-format json >"${token_create_output}" \
    || die "failed to create token ${PVE_FULL_TOKEN_ID}"
  ledger_record pve-token "${PVE_FULL_TOKEN_ID}"

  # The secret bytes move directly from the parser's stdout to this file
  # via redirection -- they are never assigned to a bash variable, never
  # passed as an argv element, and never touch this script's own
  # environment.
  PVE_TOKEN_SECRET_FILE="$(secret_tmpfile "/tmp/hubinet-ops-bootstrap-token.XXXXXX")"
  _json_string_value "${token_create_output}" "value" >"${PVE_TOKEN_SECRET_FILE}" \
    || die "could not parse the PVE token secret from 'pveum user token add' output"
  [[ -s "${PVE_TOKEN_SECRET_FILE}" ]] || die "PVE token secret was empty after creation -- refusing to continue"

  # privsep=1 means the token has NO effective permissions of its own
  # until it receives its own ACL grant (runbook section 2.4 step 4) --
  # skipping this step produces an authenticated-but-zero-privilege token.
  run_logged pveum acl modify / --tokens "${PVE_FULL_TOKEN_ID}" --roles "${PVE_ROLE}" --propagate 1 \
    || die "failed to grant role ${PVE_ROLE} to token ${PVE_FULL_TOKEN_ID} at /"
  ledger_record pve-acl-token "${PVE_FULL_TOKEN_ID}"

  _verify_effective_permissions

  log_pass "PVE identity: ${PVE_USER}, role ${PVE_ROLE} (exactly ${PVE_REQUIRED_PRIVS}), token ${PVE_TOKEN_ID} (privsep=1)"
}

# _verify_effective_permissions: exact-set proof, not a blacklist. Reads
# back the token's own effective permissions and asserts the sorted set of
# truthy privilege keys equals PVE_REQUIRED_PRIVS exactly -- catches ANY
# unexpected privilege (a missing required one, or any extra one, whatever
# its name) rather than only the specific mutation-shaped privilege names
# a fixed pattern happens to enumerate.
_verify_effective_permissions() {
  local perms_file
  perms_file="$(mktemp /tmp/hubinet-ops-bootstrap-perms.XXXXXX.json)"
  chmod 0600 "${perms_file}"
  if ! pveum user token permissions "${PVE_USER}" "${PVE_TOKEN_ID}" --path / --output-format json >"${perms_file}" 2>/dev/null; then
    rm -f "${perms_file}"
    die "could not read back effective permissions for ${PVE_FULL_TOKEN_ID}"
  fi

  # `... && parse_status=0 || parse_status=$?` rather than a plain
  # assignment: under `set -e`, a bare `actual_keys="$(cmd)"` where cmd
  # exits nonzero makes the assignment itself "fail," triggering errexit
  # and aborting the script right here -- before parse_status could ever
  # be inspected or the die() message below could run.
  local actual_keys parse_status
  actual_keys="$(_json_truthy_keys_sorted "${perms_file}")" && parse_status=0 || parse_status=$?
  rm -f "${perms_file}"
  (( parse_status == 0 )) \
    || die "could not parse effective-permission JSON for ${PVE_FULL_TOKEN_ID} (requires jq or python3 on the PVE host -- checked in phase1)"

  local expected_keys
  expected_keys="$(printf '%s\n' "${PVE_REQUIRED_PRIVS//,/$'\n'}" | LC_ALL=C sort)"

  if [[ "${actual_keys}" != "${expected_keys}" ]]; then
    local actual_csv
    actual_csv="$(printf '%s' "${actual_keys}" | tr '\n' ',' | sed 's/,$//')"
    die "verification failed: token ${PVE_FULL_TOKEN_ID} effective privileges are exactly {${actual_csv:-<none>}} but must be exactly {${PVE_REQUIRED_PRIVS}} -- refusing to continue with a credential whose privilege set is not provably read-only (this is an exact-set check: any missing required privilege OR any unexpected extra privilege fails it)"
  fi
}

# ---------------------------------------------------------------------------
# Phase 7 -- PVE TLS trust material.
#
# TLS_TRUST_MODE default is "" (unset): falling back to the CT's system
# trust store is only ever taken when the operator explicitly opts in with
# --tls-trust system. Auto-detecting the PVE cluster's own CA (or an
# explicit --pve-ca-path) remains the default, safe path and needs no
# opt-in. R0 never supports disabling TLS verification (verify=false).
# ---------------------------------------------------------------------------

phase7_tls_trust() {
  log_phase "Phase 7: PVE TLS trust"

  if [[ "${TLS_TRUST_MODE}" == "system" ]]; then
    # --pve-ca-path + --tls-trust system is already rejected in phase1
    # preflight as mutually exclusive.
    log_info "operator explicitly requested the CT's system trust store (--tls-trust system) -- skipping PVE CA auto-detection/deployment"
    CT_CA_BUNDLE_PATH=""
    log_pass "TLS trust: system trust store (explicit operator opt-in)"
    return 0
  fi

  local ca_source=""
  if [[ -n "${PVE_CA_PATH}" ]]; then
    [[ -f "${PVE_CA_PATH}" ]] || die "--pve-ca-path '${PVE_CA_PATH}' does not exist"
    ca_source="${PVE_CA_PATH}"
  else
    ca_source="$(_detect_pve_ca_bundle)" || true
  fi

  if [[ -z "${ca_source}" ]]; then
    die "could not determine PVE CA trust material and no --pve-ca-path was given. R0 never supports disabling TLS verification. Either pass --pve-ca-path <path-to-pve-root-ca.pem>, or explicitly pass --tls-trust system to opt in to relying on the target CT's system trust store (only correct if the PVE endpoint's certificate chains to a publicly-trusted CA or one you have already installed into the CT's OS trust store yourself) -- this bootstrap never falls back to system trust implicitly."
  fi

  CT_CA_BUNDLE_PATH="/etc/hubinet-ops/pve-ca.pem"
  BOOTSTRAP_CA_SOURCE_PATH="${ca_source}"
  log_pass "TLS trust: explicit CA bundle from ${ca_source} (deployed into the CT at ${CT_CA_BUNDLE_PATH})"
}

# _detect_pve_ca_bundle: the cluster's own CA certificate lives at a fixed,
# well-known path on every PVE host (used internally for pveproxy's own
# certificate chain). This is the standard, safe default source to hand to
# a new CT rather than teaching operators to hunt for it.
_detect_pve_ca_bundle() {
  local candidate="/etc/pve/pve-root-ca.pem"
  [[ -f "${candidate}" ]] && { printf '%s' "${candidate}"; return 0; }
  return 1
}
