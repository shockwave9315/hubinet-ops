#!/usr/bin/env bash
# Phase 8 -- deterministic source transfer + deploy/install-0.5.0-fresh.sh
#            invocation inside the new CT.
# Phase 8b -- required CT tooling provisioning (nftables, curl, iproute2).
# Phase 9 -- source-centric R0 config generation (inventory.yaml, agent.env).
#
# Source provenance (Mandatory Fix 4/6): this bootstrap deploys ONLY from a
# real git checkout with a clean working tree, at an exact, operator-
# confirmed full commit SHA, via `git archive <sha>` (never `HEAD` as a
# moving target, never a non-git tarball fallback -- deleted entirely).
# `git archive` transfers tracked files only, so .git/venvs/caches/logs/
# runtime DBs/untracked secrets are structurally excluded (this repository
# already enforces nothing sensitive is tracked -- scripts/check_tracked_files.py).
#
# A signed-release / `curl | bash` distribution trust chain is explicitly
# OUT OF SCOPE for this wave -- see deploy/README-bootstrap-proxmox-0.5.md
# "What remains manual". This script's guarantee is narrower and honest:
# the exact tracked-file content of one confirmed local commit is what
# gets deployed, nothing silently different.

SOURCE_HEAD_SHA=""

# _plan_source_commit: read-only. Validates the git checkout, the clean
# worktree requirement, and (if given) --expected-sha; determines the
# exact commit that WILL be deployed and records it in SOURCE_HEAD_SHA so
# it can be shown in the pre-confirmation plan. No mutation. Must run
# before confirm_or_abort.
_plan_source_commit() {
  git -C "${SOURCE_DIR}" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
    || die "SOURCE_DIR (${SOURCE_DIR}) is not a git checkout -- this bootstrap only deploys from a real git checkout so the exact deployed commit can be verified via 'git archive <confirmed-sha>'. A non-git tarball fallback is not supported."

  local dirty
  dirty="$(git -C "${SOURCE_DIR}" status --porcelain 2>/dev/null)"
  [[ -z "${dirty}" ]] \
    || die "SOURCE_DIR (${SOURCE_DIR}) has a dirty working tree (uncommitted changes) -- commit or discard your changes before running the bootstrap, so the deployed source exactly matches a real, inspectable commit."

  local head_sha
  head_sha="$(git -C "${SOURCE_DIR}" rev-parse HEAD)"
  [[ "${head_sha}" =~ ^[0-9a-f]{40}$ ]] || die "could not determine a full 40-character HEAD commit SHA for ${SOURCE_DIR}"

  if [[ -n "${EXPECTED_SOURCE_SHA}" ]]; then
    [[ "${head_sha}" == "${EXPECTED_SOURCE_SHA}" ]] \
      || die "SOURCE_DIR HEAD (${head_sha}) does not match --expected-sha (${EXPECTED_SOURCE_SHA}) -- refusing to deploy an unconfirmed commit"
  elif [[ "${BOOTSTRAP_NON_INTERACTIVE}" == "1" ]]; then
    die "--non-interactive requires --expected-sha <full-40-character-sha> so the deployed source commit is explicitly authorized rather than merely whatever HEAD happens to be at run time"
  else
    [[ -t 0 ]] || die "no --expected-sha given and no controlling terminal to confirm the detected source commit interactively"
    log_info "Detected source commit: ${head_sha}"
    local reply
    read -r -p "Deploy exactly this commit (${head_sha})? [y/N] " reply
    case "${reply}" in
      y|Y|yes|YES) : ;;
      *) die "aborted by operator -- detected source commit ${head_sha} was not confirmed" ;;
    esac
  fi

  SOURCE_HEAD_SHA="${head_sha}"
  log_info "Deploying source commit: ${SOURCE_HEAD_SHA}"
}

CT_SRC_TARBALL_HOST=""
CT_SRC_TARBALL_CT="/tmp/hubinet-ops-src.tar.gz"
CT_SRC_DIR_CT="/tmp/hubinet-ops-src"

phase8_deploy_source() {
  log_phase "Phase 8: deploy Hubinet Ops source into the container"

  [[ -n "${SOURCE_HEAD_SHA}" ]] || die "internal error: SOURCE_HEAD_SHA was not set by _plan_source_commit before phase8_deploy_source"

  # Recheck immediately before use: the confirmed commit must still be
  # exactly HEAD (fail closed if the worktree changed underneath us
  # between planning/confirmation and this mutating step).
  local recheck_sha
  recheck_sha="$(git -C "${SOURCE_DIR}" rev-parse HEAD 2>/dev/null)" || die "could not re-verify SOURCE_DIR HEAD before deployment"
  [[ "${recheck_sha}" == "${SOURCE_HEAD_SHA}" ]] \
    || die "SOURCE_DIR HEAD changed from the confirmed commit (${SOURCE_HEAD_SHA}) to ${recheck_sha} between confirmation and deployment -- refusing to deploy an unconfirmed commit"
  local recheck_dirty
  recheck_dirty="$(git -C "${SOURCE_DIR}" status --porcelain 2>/dev/null)"
  [[ -z "${recheck_dirty}" ]] \
    || die "SOURCE_DIR became dirty between confirmation and deployment -- refusing to deploy"

  log_info "Deploying source commit: ${SOURCE_HEAD_SHA}"

  CT_SRC_TARBALL_HOST="$(secret_tmpfile "/tmp/hubinet-ops-bootstrap-src.XXXXXX.tar.gz")"
  # Not actually secret content, but reusing secret_tmpfile gets us a
  # restrictive 0600 temp file and guaranteed cleanup on exit for free.

  run_logged git -C "${SOURCE_DIR}" archive "${SOURCE_HEAD_SHA}" -o "${CT_SRC_TARBALL_HOST}" \
    || die "git archive of commit ${SOURCE_HEAD_SHA} in ${SOURCE_DIR} failed"

  run_logged pct push "${VMID}" "${CT_SRC_TARBALL_HOST}" "${CT_SRC_TARBALL_CT}" \
    || die "pct push of source tarball into container ${VMID} failed"

  run_logged pct exec "${VMID}" -- mkdir -p "${CT_SRC_DIR_CT}" \
    || die "failed to create ${CT_SRC_DIR_CT} inside container ${VMID}"
  run_logged pct exec "${VMID}" -- tar -xzf "${CT_SRC_TARBALL_CT}" -C "${CT_SRC_DIR_CT}" \
    || die "failed to extract source tarball inside container ${VMID}"
  run_logged pct exec "${VMID}" -- rm -f "${CT_SRC_TARBALL_CT}" \
    || log_warn "could not remove ${CT_SRC_TARBALL_CT} inside the container (non-fatal)"

  log_info "invoking deploy/install-0.5.0-fresh.sh inside container ${VMID}"
  run_logged pct exec "${VMID}" -- bash "${CT_SRC_DIR_CT}/deploy/install-0.5.0-fresh.sh" \
    || die "deploy/install-0.5.0-fresh.sh failed inside container ${VMID} -- see output above; the installer's own fresh-host checks were not weakened by this bootstrap"

  log_pass "source deployed (commit ${SOURCE_HEAD_SHA}) and installer completed inside container ${VMID}"
}

# phase8b_provision_tooling: this bootstrap's own later phases depend on
# nftables (phase10 firewall), curl (unauthenticated health probe in
# phase12), and iproute2/`ss` (listener checks in phase12) being present
# inside the CT. The base Debian 13 standard template does not guarantee
# any of these are pre-installed -- they are explicitly provisioned here,
# after the installer has run, rather than assumed.
phase8b_provision_tooling() {
  log_phase "Phase 8b: provision required CT tooling (nftables, curl, iproute2)"

  run_logged pct exec "${VMID}" -- apt-get update \
    || die "apt-get update failed inside container ${VMID} while provisioning required tooling"
  run_logged pct exec "${VMID}" -- env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends nftables curl iproute2 \
    || die "failed to install required tooling (nftables, curl, iproute2) inside container ${VMID}"

  _require_ct_command nft
  _require_ct_command curl
  _require_ct_command ss

  log_pass "required CT tooling present: nftables (nft), curl, iproute2 (ss)"
}

_require_ct_command() {
  local cmd="$1"
  pct exec "${VMID}" -- sh -c "command -v ${cmd}" >/dev/null 2>&1 \
    || die "required command '${cmd}' is still not available inside container ${VMID} after provisioning"
}

phase9_generate_config() {
  log_phase "Phase 9: generate source-centric R0 configuration"

  local tls_block
  if [[ -n "${CT_CA_BUNDLE_PATH}" ]]; then
    if [[ -n "${BOOTSTRAP_CA_SOURCE_PATH:-}" ]]; then
      run_logged pct push "${VMID}" "${BOOTSTRAP_CA_SOURCE_PATH}" "${CT_CA_BUNDLE_PATH}" \
        || die "failed to push PVE CA bundle into container ${VMID}"
      run_logged pct exec "${VMID}" -- chown root:hubinetops "${CT_CA_BUNDLE_PATH}" \
        || die "failed to chown PVE CA bundle inside container ${VMID}"
      run_logged pct exec "${VMID}" -- chmod 0640 "${CT_CA_BUNDLE_PATH}" \
        || die "failed to chmod PVE CA bundle inside container ${VMID}"
    fi
    tls_block=$'  tls:\n    verify: true\n    ca_bundle_path: "'"${CT_CA_BUNDLE_PATH}"'"'
  else
    tls_block=$'  tls:\n    verify: true\n    ca_bundle_path: null'
  fi

  local inventory_tmp
  inventory_tmp="$(secret_tmpfile "/tmp/hubinet-ops-bootstrap-inventory.XXXXXX.yaml")"
  cat >"${inventory_tmp}" <<YAML
# Generated by deploy/bootstrap-proxmox-0.5.sh -- source-centric only.
# No VMID/LXC/QEMU list exists here or anywhere else in this file; every
# node/resource is discovered dynamically after this bootstrap completes.
source:
  display_name: "${HA_DISPLAY_NAME}"
  provider_kind: proxmox_ve
  pve_endpoint: "${PVE_ENDPOINT}"
  freshness_duration_seconds: ${FRESHNESS_SECONDS}
  credential_reference: "secret://${PVE_TOKEN_ID}-v1"
  pve_token_env: HUBINET_OPS_R0_PVE_TOKEN
${tls_block}

runtime:
  authority_db_path: /var/lib/hubinet-ops/authority.db
  api_token_env: HUBINET_OPS_R0_API_TOKEN
YAML

  run_logged pct push "${VMID}" "${inventory_tmp}" /etc/hubinet-ops/inventory.yaml \
    || die "failed to push generated inventory.yaml into container ${VMID}"
  run_logged pct exec "${VMID}" -- chown root:hubinetops /etc/hubinet-ops/inventory.yaml \
    || die "failed to chown inventory.yaml inside container ${VMID}"
  run_logged pct exec "${VMID}" -- chmod 0640 /etc/hubinet-ops/inventory.yaml \
    || die "failed to chmod inventory.yaml inside container ${VMID}"

  # agent.env: install-0.5.0-fresh.sh already generated a fresh
  # HUBINET_OPS_R0_API_TOKEN inside the container (§ its own step 2). We
  # only need to fill in HUBINET_OPS_R0_PVE_TOKEN -- read the existing
  # file back, replace exactly that one line, never regenerate the API
  # bearer token the installer already created.
  #
  # Secret handling: the PVE token is read by awk itself, via `getline`
  # from PVE_TOKEN_SECRET_FILE (a path -- never secret), inside the awk
  # program's own BEGIN block. The token value is never passed to awk as
  # a -v argument (which would put it in awk's own argv, visible via
  # /proc/<pid>/cmdline for the duration of the call) and never assigned
  # to a bash variable in this script.
  local existing_env_tmp
  existing_env_tmp="$(secret_tmpfile "/tmp/hubinet-ops-bootstrap-agentenv-read.XXXXXX")"
  run_logged pct exec "${VMID}" -- cat /etc/hubinet-ops/agent.env >"${existing_env_tmp}" \
    || die "failed to read /etc/hubinet-ops/agent.env from container ${VMID} (was it created by install-0.5.0-fresh.sh?)"

  local new_env_tmp
  new_env_tmp="$(secret_tmpfile "/tmp/hubinet-ops-bootstrap-agentenv-write.XXXXXX")"
  awk -v token_file="${PVE_TOKEN_SECRET_FILE}" -v full_token_id="${PVE_FULL_TOKEN_ID}" '
    BEGIN {
      getline token < token_file
      close(token_file)
    }
    /^HUBINET_OPS_R0_PVE_TOKEN=/ { print "HUBINET_OPS_R0_PVE_TOKEN=" full_token_id "=" token; next }
    { print }
  ' "${existing_env_tmp}" >"${new_env_tmp}"

  grep -q '^HUBINET_OPS_R0_PVE_TOKEN=.\+=.\+$' "${new_env_tmp}" \
    || die "failed to write HUBINET_OPS_R0_PVE_TOKEN into the generated agent.env (internal error -- no secret was logged)"
  grep -q '^HUBINET_OPS_R0_API_TOKEN=.\+$' "${new_env_tmp}" \
    || die "agent.env does not contain a non-empty HUBINET_OPS_R0_API_TOKEN after config generation (was it generated by install-0.5.0-fresh.sh?)"

  run_logged_redacted "pct push <container> <redacted agent.env> /etc/hubinet-ops/agent.env" \
    pct push "${VMID}" "${new_env_tmp}" /etc/hubinet-ops/agent.env \
    || die "failed to push updated agent.env into container ${VMID}"
  run_logged pct exec "${VMID}" -- chown root:hubinetops /etc/hubinet-ops/agent.env \
    || die "failed to chown agent.env inside container ${VMID}"
  run_logged pct exec "${VMID}" -- chmod 0640 /etc/hubinet-ops/agent.env \
    || die "failed to chmod agent.env inside container ${VMID}"

  log_pass "generated source-centric inventory.yaml and agent.env (0640, root:hubinetops); secrets were never assigned to a shell variable or passed as a command argument in this script"
}
