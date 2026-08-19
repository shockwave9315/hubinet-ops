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

PVE_USER="hubinetops@pve"
PVE_ROLE="HubinetOpsR0Auditor"
PVE_TOKEN_ID="r0-readonly"
PVE_FULL_TOKEN_ID="${PVE_USER}!${PVE_TOKEN_ID}"
PVE_REQUIRED_PRIVS="Sys.Audit,VM.Audit"

# Privileges that must NEVER appear in the verified effective-permission
# set. Matched as a fixed-string/regex "contains" check against each
# privilege in the output -- not an exhaustive enumeration of every
# possible PVE mutation privilege, but every class of privilege the
# operational-activation runbook (section 2.3) explicitly forbids.
_PVE_FORBIDDEN_PRIV_PATTERN='(\.Config|\.Modify|\.Allocate|\.PowerMgmt|\.Monitor|\.Backup|Realm\.|User\.)'

phase6_pve_identity() {
  log_phase "Phase 6: dedicated read-only PVE identity"

  pveum user list --output-format json 2>/dev/null | _json_contains_field "userid" "${PVE_USER}" \
    && die "PVE user '${PVE_USER}' already exists -- this bootstrap only performs a fresh install and will not reuse an existing identity of unknown provenance. Remove it yourself (pveum user delete ${PVE_USER}) after confirming it is not depended on elsewhere, then retry."

  pveum role list --output-format json 2>/dev/null | _json_contains_field "roleid" "${PVE_ROLE}" \
    && die "PVE role '${PVE_ROLE}' already exists -- refusing to reuse it (unknown provenance/privilege history). Remove it yourself (pveum role delete ${PVE_ROLE}) after confirming it is not depended on elsewhere, then retry."

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

  PVE_TOKEN_SECRET_FILE="$(secret_tmpfile "/tmp/hubinet-ops-bootstrap-token.XXXXXX")"
  _json_field_value "$(cat "${token_create_output}")" "value" >"${PVE_TOKEN_SECRET_FILE}" \
    || die "could not parse the PVE token secret from 'pveum user token add' output"
  [[ -s "${PVE_TOKEN_SECRET_FILE}" ]] || die "PVE token secret was empty after creation -- refusing to continue"

  # privsep=1 means the token has NO effective permissions of its own
  # until it receives its own ACL grant (runbook section 2.4 step 4) --
  # skipping this step produces an authenticated-but-zero-privilege token.
  run_logged pveum acl modify / --tokens "${PVE_FULL_TOKEN_ID}" --roles "${PVE_ROLE}" --propagate 1 \
    || die "failed to grant role ${PVE_ROLE} to token ${PVE_FULL_TOKEN_ID} at /"
  ledger_record pve-acl-token "${PVE_FULL_TOKEN_ID}"

  _verify_effective_permissions

  log_pass "PVE identity: ${PVE_USER}, role ${PVE_ROLE} (Sys.Audit+VM.Audit only), token ${PVE_TOKEN_ID} (privsep=1)"
}

_verify_effective_permissions() {
  local perms
  perms="$(pveum user token permissions "${PVE_USER}" "${PVE_TOKEN_ID}" --path / --output-format json 2>/dev/null)" \
    || die "could not read back effective permissions for ${PVE_FULL_TOKEN_ID}"

  local priv
  local -a required=("Sys.Audit" "VM.Audit")
  for priv in "${required[@]}"; do
    _json_contains_field_anywhere "${perms}" "${priv}" \
      || die "verification failed: token ${PVE_FULL_TOKEN_ID} does not have required privilege '${priv}' after ACL grant -- check pveum acl list / pveum role list on the PVE host"
  done

  if [[ "${perms}" =~ ${_PVE_FORBIDDEN_PRIV_PATTERN} ]]; then
    die "verification failed: token ${PVE_FULL_TOKEN_ID} carries a mutation-shaped privilege (matched pattern: ${_PVE_FORBIDDEN_PRIV_PATTERN}) -- refusing to continue with an over-privileged credential"
  fi
}

# ---------------------------------------------------------------------------
# Minimal JSON helpers -- prefer jq, fall back to python3, fall back to a
# conservative regex extractor for the flat single-level objects `pveum
# --output-format json` emits for these specific subcommands. No hard
# dependency on either jq or python3 being installed on the PVE host.
# ---------------------------------------------------------------------------

_json_field_value() {
  local json="$1" field="$2"
  if command -v jq >/dev/null 2>&1; then
    printf '%s' "${json}" | jq -r --arg f "${field}" '.[$f]'
    return
  fi
  if command -v python3 >/dev/null 2>&1; then
    python3 -c 'import json,sys; d=json.loads(sys.argv[1]); print(d[sys.argv[2]])' "${json}" "${field}" 2>/dev/null
    return
  fi
  printf '%s' "${json}" | grep -o "\"${field}\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" | head -n1 | sed -E 's/.*:"([^"]*)"/\1/'
}

# _json_contains_field: true if a JSON array (list output, e.g. `pveum user
# list --output-format json`) contains an object whose given field equals
# the given value.
_json_contains_field() {
  local field="$1" value="$2"
  local json
  json="$(cat)"
  if command -v jq >/dev/null 2>&1; then
    printf '%s' "${json}" | jq -e --arg f "${field}" --arg v "${value}" 'any(.[]; .[$f] == $v)' >/dev/null 2>&1
    return
  fi
  if command -v python3 >/dev/null 2>&1; then
    python3 -c '
import json, sys
data = json.loads(sys.argv[1])
field, value = sys.argv[2], sys.argv[3]
sys.exit(0 if any(row.get(field) == value for row in data) else 1)
' "${json}" "${field}" "${value}" 2>/dev/null
    return
  fi
  printf '%s' "${json}" | grep -q "\"${field}\"[[:space:]]*:[[:space:]]*\"${value}\""
}

# _json_contains_field_anywhere: true if `needle` appears as a value string
# anywhere in the JSON blob (used for the flat effective-permissions object,
# whose keys ARE the privilege names, e.g. {"Sys.Audit":1,"VM.Audit":1}).
_json_contains_field_anywhere() {
  local json="$1" needle="$2"
  printf '%s' "${json}" | grep -q "\"${needle}\""
}

# ---------------------------------------------------------------------------
# Phase 7 -- PVE TLS trust material.
# ---------------------------------------------------------------------------

phase7_tls_trust() {
  log_phase "Phase 7: PVE TLS trust"

  local ca_source=""
  if [[ -n "${PVE_CA_PATH}" ]]; then
    [[ -f "${PVE_CA_PATH}" ]] || die "--pve-ca-path '${PVE_CA_PATH}' does not exist"
    ca_source="${PVE_CA_PATH}"
  else
    ca_source="$(_detect_pve_ca_bundle)" || true
  fi

  if [[ -z "${ca_source}" ]]; then
    if [[ "${TLS_TRUST_MODE}" == "system" ]]; then
      log_info "no explicit PVE CA bundle configured or found; relying on the target CT's system trust store (source.tls.ca_bundle_path will be null). This is only correct if the PVE endpoint's certificate chains to a publicly-trusted CA or one already installed in the CT's OS trust store."
      CT_CA_BUNDLE_PATH=""
      log_pass "TLS trust: system trust store"
      return 0
    fi
    die "could not determine PVE CA trust material and no --pve-ca-path was given; R0 never supports disabling TLS verification -- provide --pve-ca-path or install the PVE root CA into the target CT's system trust store yourself before re-running with --tls-trust system"
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
