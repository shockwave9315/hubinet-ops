#!/usr/bin/env bash
set -Eeuo pipefail

# Hubinet Ops 0.5 -- in-place PRODUCT UPDATE for an EXISTING installation.
#
# INSTALL ONCE -> UPDATE MANY TIMES. deploy/bootstrap-proxmox-0.5.sh
# remains the first-install / disaster-recovery / deliberate-rebuild
# entrypoint; it is never re-run against an installation this script is
# meant to update. This script never invokes
# deploy/install-0.5.0-fresh.sh, never recreates the LXC, never changes
# its VMID/network, never rotates the PVE identity/token secret/HA
# bearer, never regenerates inventory.yaml/agent.env/TLS trust/the
# host-control key/nftables, and never rewrites Home Assistant enrollment
# -- see AGENTS.md and deploy/README-update-proxmox-0.5.md.
#
# Run as root ON the Proxmox VE host itself, exactly like the bootstrap
# script -- this updater needs legitimate control over both the Hubinet CT
# (via `pct`) and the PVE-host helper/authorized_keys context. It does not
# add write/update capability to the runtime forced-command SSH channel,
# and it does not turn package-scan host control into general remote
# deployment control.
#
# See deploy/README-update-proxmox-0.5.md for the full operator runbook.

UPDATE_SCRIPT_DIR_FULL="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Reuses the exact global name deploy/lib/bootstrap-*.sh already expect
# (e.g. bootstrap-finish.sh's CT_ACCEPT_SCRIPT_CT push path) so those
# files can be sourced unmodified.
BOOTSTRAP_SCRIPT_DIR="${UPDATE_SCRIPT_DIR_FULL}"
UPDATE_SCRIPT_DIR="${BOOTSTRAP_SCRIPT_DIR}/lib"

# shellcheck source=lib/bootstrap-common.sh
source "${BOOTSTRAP_SCRIPT_DIR}/lib/bootstrap-common.sh"
# shellcheck source=lib/bootstrap-identity.sh
source "${BOOTSTRAP_SCRIPT_DIR}/lib/bootstrap-identity.sh"
# shellcheck source=lib/bootstrap-host-control.sh
source "${BOOTSTRAP_SCRIPT_DIR}/lib/bootstrap-host-control.sh"
# shellcheck source=lib/bootstrap-deploy.sh
source "${BOOTSTRAP_SCRIPT_DIR}/lib/bootstrap-deploy.sh"
# shellcheck source=lib/bootstrap-finish.sh
source "${BOOTSTRAP_SCRIPT_DIR}/lib/bootstrap-finish.sh"
# shellcheck source=lib/update-ownership.sh
source "${BOOTSTRAP_SCRIPT_DIR}/lib/update-ownership.sh"
# shellcheck source=lib/update-plan.sh
source "${BOOTSTRAP_SCRIPT_DIR}/lib/update-plan.sh"
# shellcheck source=lib/update-stage.sh
source "${BOOTSTRAP_SCRIPT_DIR}/lib/update-stage.sh"
# shellcheck source=lib/update-activate.sh
source "${BOOTSTRAP_SCRIPT_DIR}/lib/update-activate.sh"
# shellcheck source=lib/update-recovery.sh
source "${BOOTSTRAP_SCRIPT_DIR}/lib/update-recovery.sh"

# ---------------------------------------------------------------------------
# Defaults / CLI contract (AGENTS.md task prompt section 8)
# ---------------------------------------------------------------------------

VMID=""
SOURCE_DIR="$(cd "${BOOTSTRAP_SCRIPT_DIR}/.." && pwd)"
EXPECTED_SOURCE_SHA=""
BOOTSTRAP_NON_INTERACTIVE="0"
BOOTSTRAP_ASSUME_YES="0"
UPDATE_ALLOW_AUTHORITY_RESET="0"
UPDATE_DRY_RUN="0"
HUBINET_OPS_TEST_MODE="${HUBINET_OPS_TEST_MODE:-0}"
BOOTSTRAP_SERVICE_TIMEOUT_SECONDS="${BOOTSTRAP_SERVICE_TIMEOUT_SECONDS:-60}"
BOOTSTRAP_DISCOVERY_TIMEOUT_SECONDS="${BOOTSTRAP_DISCOVERY_TIMEOUT_SECONDS:-180}"

usage() {
  cat <<'USAGE'
Usage: update-proxmox-0.5.sh --vmid <N> [options]

In-place update of an EXISTING Hubinet Ops 0.5 installation. Requires an
existing installation created by deploy/bootstrap-proxmox-0.5.sh -- this is
not a second bootstrap and never regenerates identity, config, or secrets.

Required:
  --vmid <N>                  the existing Hubinet CT's VMID. This updater
                               proves the CT at this VMID is the expected
                               Hubinet installation before touching
                               anything; it does not auto-discover it.

Commonly used:
  --source-dir <path>          default: this script's own repository root
                               (must be a git checkout with a clean
                               working tree, at one exact confirmed commit
                               -- there is no non-git deployment path).
  --expected-sha <full-sha>    the exact 40-character git commit SHA this
                               run is authorized to update to; required in
                               --non-interactive mode, optional (but
                               recommended) otherwise, where the detected
                               HEAD SHA is printed and must be confirmed
                               interactively instead.
  --dry-run                    inspect, classify, and print the exact
                               update plan; makes zero managed-state
                               mutations.

Behavior:
  --non-interactive            fail closed instead of prompting for any
                               missing required value (including
                               --expected-sha)
  --yes, -y                    skip the ORDINARY plan confirmation only --
                               never skips source-provenance validation
                               and never, by itself, authorizes a
                               destructive authority reset
  --allow-authority-reset      explicit authorization for a destructive
                               authority-database reset in
                               --non-interactive mode; must be combined
                               with --yes. Interactive runs are instead
                               asked a second, dedicated confirmation.
  -h, --help                    show this help and exit

This updater never invokes deploy/install-0.5.0-fresh.sh, never recreates
the LXC or changes its VMID/network, and never rotates the PVE identity/
token secret, the HA bearer, or the host-control key. An incompatible
prerelease authority schema may require an explicit, backed-up authority
database reset; the Hubinet installation itself is never destroyed. See
deploy/README-update-proxmox-0.5.md.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --vmid) VMID="$2"; shift 2 ;;
    --source-dir) SOURCE_DIR="$2"; shift 2 ;;
    --expected-sha) EXPECTED_SOURCE_SHA="$2"; shift 2 ;;
    --non-interactive) BOOTSTRAP_NON_INTERACTIVE="1"; shift ;;
    --yes|-y) BOOTSTRAP_ASSUME_YES="1"; shift ;;
    --allow-authority-reset) UPDATE_ALLOW_AUTHORITY_RESET="1"; shift ;;
    --dry-run) UPDATE_DRY_RUN="1"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
done

[[ -n "${VMID}" ]] || die "--vmid is required (see --help)"
is_valid_vmid "${VMID}" || die "--vmid '${VMID}' is not a valid Proxmox VMID"

if [[ "${HUBINET_OPS_TEST_MODE}" != "1" ]]; then
  [[ "${EUID}" -eq 0 ]] || die "must run as root on the Proxmox host"
fi

require_command pct "Proxmox container control"
require_command pveum "Proxmox user/permission management"
require_command git "source commit verification"
require_command python3 "JSON parsing and authority database inspection"
require_command cmp "byte-exact update-plan artifact comparison"
require_command flock "per-VMID updater single-flight"
require_command sync "durable interrupted-update journal replacement"

# ---------------------------------------------------------------------------
# Ledger / secret-file lifecycle -- exactly the same mechanism bootstrap
# uses (deploy/lib/bootstrap-common.sh), so update_rollback_on_failure can
# use the same ledger_record/ledger_has primitives.
# ---------------------------------------------------------------------------

BOOTSTRAP_LEDGER="$(mktemp /tmp/hubinet-ops-update-ledger.XXXXXX)"
BOOTSTRAP_SECRET_FILES="$(mktemp /tmp/hubinet-ops-update-secrets.XXXXXX)"
chmod 0600 "${BOOTSTRAP_LEDGER}" "${BOOTSTRAP_SECRET_FILES}"

UPDATE_RUN_ID=""

_update_exit_trap() {
  local exit_code=$?
  trap - EXIT
  if [[ "${_UPDATE_STARTUP_RECOVERY_IN_PROGRESS:-0}" == "1" ]]; then
    log_warn "startup recovery did not complete; preserving ${UPDATE_JOURNAL_PATH} and every referenced artifact for manual recovery"
    cleanup_secret_files
    rm -f -- "${BOOTSTRAP_LEDGER}"
    exit "${exit_code}"
  fi
  if (( exit_code != 0 )) && ledger_has update-service-stop-attempted "${VMID}"; then
    update_rollback_on_failure "${exit_code}"
  elif (( exit_code != 0 )); then
    log_warn "update did not complete (exit ${exit_code}) before any service stop was attempted -- the existing installation was never touched"
    if [[ "${UPDATE_JOURNAL_STATE:-}" == "active" ]]; then
      if _update_prove_service_active_and_healthy; then
        update_journal_resolve recovered
      else
        update_stage_cleanup 2>/dev/null || true
        log_warn "the pre-mutation service did not prove active + healthy; preserving ${UPDATE_JOURNAL_PATH} for the next recovery invocation"
      fi
    else
      update_stage_cleanup 2>/dev/null || true
    fi
  fi
  cleanup_secret_files
  rm -f -- "${BOOTSTRAP_LEDGER}"
  exit "${exit_code}"
}

trap _update_exit_trap EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# ---------------------------------------------------------------------------
# Orchestration -- fixed order, never reordered. Nothing before
# update_plan_confirm mutates managed state; nothing before
# update_activate_and_accept's own recheck/stop touches the live
# installation.
# ---------------------------------------------------------------------------

# Single-flight covers dry-run and the complete recovery/update lifecycle.
# Kernel ownership of UPDATE_LOCK_FD is released automatically on shell exit,
# including after SIGKILL/reboot; the stable lock file itself is not state.
update_lock_acquire

# A prior active journal is resolved before a new run-id is allocated and
# before any ownership/planning reads for the requested new update.
update_startup_recovery_gate

# Generated once for the new invocation. The first active journal is written
# immediately after ownership is proven and before planning begins.
UPDATE_RUN_ID="$(_generate_run_id)"
update_ownership_verify "${VMID}"
UPDATE_INSTALLATION_RUN_ID="${UPDATE_VMID_RUN_ID}"
_update_set_run_paths
update_journal_checkpoint active
_plan_source_commit
update_plan_classify
update_journal_checkpoint active
update_plan_print

if [[ "${UPDATE_DRY_RUN}" == "1" ]]; then
  log_info "--dry-run: stopping after the plan above. No managed-state mutation was made."
  _update_cleanup_plan_tools
  update_journal_checkpoint completed
  _update_journal_clear
  exit 0
fi

update_plan_confirm
update_stage_all
update_activate_and_accept

exit 0
