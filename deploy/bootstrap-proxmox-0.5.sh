#!/usr/bin/env bash
set -Eeuo pipefail

# Hubinet Ops 0.5 R0 -- one-shot Proxmox bootstrap.
#
# Automates the manual procedure documented in
# deploy/README-bootstrap-proxmox-0.5.md: creates a
# fresh unprivileged Debian 13 LXC, deploys the inventory/package-scan runtime into
# it (deploy/install-0.5.0-fresh.sh, unmodified), provisions a
# least-privilege PVE read-only identity (exactly Sys.Audit+VM.Audit,
# verified as an exact set -- never a broader privilege), provisions PVE
# TLS trust material (never verify=false), applies the mandatory nftables
# egress/ingress boundary, and only then starts the service, verifies a
# real authenticated discovery success, and enables CT boot -- in that
# order, never reversed.
#
# The PVE API credential remains GET-only. Package scanning uses a separate
# forced-command SSH key and fixed `pct exec` shapes for non-installing APT
# metadata refresh/simulation; no arbitrary remote shell is exposed.
#
# Run as root ON the Proxmox VE host itself (this script drives `pct`/
# `pveum`/`pveam`/`nft` locally; it is not SSH-orchestrated).
#
# NO HOST MUTATION OF ANY KIND (including `pveam update`/`download`)
# happens before the operator has seen and confirmed the full plan
# (VMID, template, source commit) -- see the single confirm_or_abort call
# near the bottom of this file. Everything before it is read-only
# planning; everything after it may mutate.
#
# See deploy/README-bootstrap-proxmox-0.5.md for the full
# reference procedure this automates, including everything an operator
# may still want to inspect/verify by hand.

BOOTSTRAP_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/bootstrap-common.sh
source "${BOOTSTRAP_SCRIPT_DIR}/lib/bootstrap-common.sh"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

# VMID is intentionally empty by default: phase1_preflight auto-detects the
# next free VMID via the real Proxmox `/cluster/nextid` API and shows it in
# the plan. An operator-supplied --vmid is validated and never silently
# overridden. See VMID_EXPLICIT below.
VMID=""
VMID_EXPLICIT="0"
HOSTNAME_="hubinet-ops"
STORAGE=""
TEMPLATE=""
TEMPLATE_STORAGE="local"
BRIDGE="vmbr0"
NETWORK_MODE="dhcp"
STATIC_IP_CIDR=""
STATIC_GATEWAY=""
CORES="1"
MEMORY_MIB="1024"
SWAP_MIB="512"
ROOTFS_GIB="8"
LXC_FEATURES=""
HA_SOURCE_CIDR=""
PVE_ENDPOINT=""
PVE_CA_PATH=""
# TLS_TRUST_MODE default is "" (unset), NOT "system": relying on the CT's
# system trust store is only ever taken when the operator explicitly opts
# in with --tls-trust system (see bootstrap-identity.sh phase7). An
# implicit silent fallback to system trust is exactly what this default
# must never produce.
TLS_TRUST_MODE=""
DNS_RESOLVER_IP=""
FRESHNESS_SECONDS="300"
PACKAGE_SCAN_INTERVAL_SECONDS="21600"
HA_DISPLAY_NAME="Home Proxmox"
SOURCE_DIR="$(cd "${BOOTSTRAP_SCRIPT_DIR}/.." && pwd)"
EXPECTED_SOURCE_SHA=""
BOOTSTRAP_ASSUME_YES="0"
BOOTSTRAP_NON_INTERACTIVE="0"
BOOTSTRAP_CLEANUP_ON_FAILURE="0"
BOOTSTRAP_NET_TIMEOUT_SECONDS="${BOOTSTRAP_NET_TIMEOUT_SECONDS:-120}"
BOOTSTRAP_SERVICE_TIMEOUT_SECONDS="${BOOTSTRAP_SERVICE_TIMEOUT_SECONDS:-60}"
BOOTSTRAP_DISCOVERY_TIMEOUT_SECONDS="${BOOTSTRAP_DISCOVERY_TIMEOUT_SECONDS:-180}"
HUBINET_OPS_TEST_MODE="${HUBINET_OPS_TEST_MODE:-0}"

CT_IP=""
CT_CA_BUNDLE_PATH=""
BOOTSTRAP_CA_SOURCE_PATH=""
PVE_TOKEN_SECRET_FILE=""
DISCOVERY_ACCEPTANCE_SUMMARY=""

# A random per-invocation identifier, embedded into every PVE object
# comment this bootstrap creates that supports one (user, token) -- the
# ownership-proof mechanism bootstrap-identity.sh's rollback functions use
# to distinguish "this run created it" from "an object matching our fixed
# name merely exists" after an ambiguous mutate-then-error PVE result
# (e.g. a concurrent bootstrap run racing for the same fixed name). See
# bootstrap-identity.sh's top-of-file comment and
# bootstrap-common.sh::_generate_run_id.
BOOTSTRAP_RUN_ID="$(_generate_run_id)"

usage() {
  cat <<'USAGE'
Usage: bootstrap-proxmox-0.5.sh [options]

Creates a fresh unprivileged Debian 13 LXC, deploys the Hubinet Ops 0.5
inventory/package-scan runtime into it, provisions a least-privilege PVE credential and
the mandatory firewall boundary, and starts the service.

Required:
  --ha-source <CIDR>        Home Assistant host/subnet allowed to reach
                             TCP 8787 (e.g. 203.0.113.50/32). No default --
                             this must be explicit.

Commonly overridden:
  --vmid <N>                  default: auto-detected next free VMID
                               (pvesh get /cluster/nextid); an explicit
                               value here is validated and never silently
                               overridden, including on a creation-time
                               race.
  --hostname <name>           default: hubinet-ops
  --storage <name>            default: auto-detected
  --template <volid>          default: auto-selected/downloaded newest
                               local Debian 13 standard template (newest
                               across ALL storages, not just the last one
                               checked)
  --bridge <name>              default: vmbr0
  --network dhcp|static        default: dhcp
  --ip <address/prefix>        required if --network static
  --gateway <address>          required if --network static
  --pve-endpoint <url>         default: auto-detected https://<host-ip>:8006
  --pve-ca-path <path>         default: auto-detected /etc/pve/pve-root-ca.pem
  --tls-trust system            explicit opt-in to the target CT's system
                               trust store when no CA bundle is found or
                               given; there is no implicit default for
                               this -- omitting both --pve-ca-path and
                               --tls-trust fails closed if no CA is found.
  --dns-resolver <ip>          required only if --pve-endpoint uses a
                               hostname rather than a literal IP
  --expected-sha <full-sha>    the exact 40-character git commit SHA this
                               run is authorized to deploy from
                               --source-dir; required in --non-interactive
                               mode, optional (but recommended) otherwise,
                               where the detected HEAD SHA is printed and
                               must be confirmed interactively instead.
  --discovery-timeout <sec>    default: 180 -- how long phase 12 polls
                               GET /r0/v1/snapshot for a genuinely healthy,
                               provenance-verified discovery result before
                               failing (see Mandatory acceptance below).
  --freshness-seconds <N>      default: 300
  --package-scan-interval <N>  default: 21600 (6 hours)
  --display-name <text>        default: "Home Proxmox"
  --source-dir <path>          default: this script's own repository root
                               (must be a git checkout with a clean
                               working tree -- see --expected-sha above;
                               there is no non-git deployment path).

Resources:
  --cores <N>                default: 1
  --memory <MiB>              default: 1024
  --swap <MiB>                 default: 512
  --rootfs-size <GiB>          default: 8

Behavior:
  --non-interactive           fail closed instead of prompting for any
                               missing required value (including
                               --expected-sha)
  --yes, -y                    skip interactive confirmations (does not
                               skip the --expected-sha requirement in
                               --non-interactive mode)
  --cleanup-on-failure          destroy the container automatically if
                               bootstrap fails after creating it (default:
                               preserve it for forensic diagnosis)
  -h, --help                    show this help and exit

PVE identity (user hubinetops@pve, role HubinetOpsR0Auditor, token
r0-readonly, privileges exactly Sys.Audit,VM.Audit) and the nftables
firewall boundary are always created; there are no flags to weaken or
skip them. No host mutation of any kind (including `pveam update`/
`download`) happens before the operator confirms the full plan.
USAGE
}

# ---------------------------------------------------------------------------
# Argument parsing -- no eval, no arbitrary command construction from
# user input; every value below is used only as a quoted argv element or
# validated against a fixed pattern before use.
# ---------------------------------------------------------------------------

while [[ $# -gt 0 ]]; do
  case "$1" in
    --vmid) VMID="$2"; shift 2 ;;
    --hostname) HOSTNAME_="$2"; shift 2 ;;
    --storage) STORAGE="$2"; shift 2 ;;
    --template) TEMPLATE="$2"; shift 2 ;;
    --template-storage) TEMPLATE_STORAGE="$2"; shift 2 ;;
    --bridge) BRIDGE="$2"; shift 2 ;;
    --network) NETWORK_MODE="$2"; shift 2 ;;
    --ip) STATIC_IP_CIDR="$2"; shift 2 ;;
    --gateway) STATIC_GATEWAY="$2"; shift 2 ;;
    --cores) CORES="$2"; shift 2 ;;
    --memory) MEMORY_MIB="$2"; shift 2 ;;
    --swap) SWAP_MIB="$2"; shift 2 ;;
    --rootfs-size) ROOTFS_GIB="$2"; shift 2 ;;
    --ha-source) HA_SOURCE_CIDR="$2"; shift 2 ;;
    --pve-endpoint) PVE_ENDPOINT="$2"; shift 2 ;;
    --pve-ca-path) PVE_CA_PATH="$2"; shift 2 ;;
    --tls-trust) TLS_TRUST_MODE="$2"; shift 2 ;;
    --dns-resolver) DNS_RESOLVER_IP="$2"; shift 2 ;;
    --expected-sha) EXPECTED_SOURCE_SHA="$2"; shift 2 ;;
    --discovery-timeout) BOOTSTRAP_DISCOVERY_TIMEOUT_SECONDS="$2"; shift 2 ;;
    --freshness-seconds) FRESHNESS_SECONDS="$2"; shift 2 ;;
    --package-scan-interval) PACKAGE_SCAN_INTERVAL_SECONDS="$2"; shift 2 ;;
    --display-name) HA_DISPLAY_NAME="$2"; shift 2 ;;
    --source-dir) SOURCE_DIR="$2"; shift 2 ;;
    --non-interactive) BOOTSTRAP_NON_INTERACTIVE="1"; shift ;;
    --yes|-y) BOOTSTRAP_ASSUME_YES="1"; shift ;;
    --cleanup-on-failure) BOOTSTRAP_CLEANUP_ON_FAILURE="1"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
done

# ---------------------------------------------------------------------------
# Interactive fill-in for missing required values (only --ha-source has no
# safe default). Skipped entirely in --non-interactive mode, which fails
# closed instead if a required value is still missing.
# ---------------------------------------------------------------------------

if [[ -z "${HA_SOURCE_CIDR}" ]]; then
  if [[ "${BOOTSTRAP_NON_INTERACTIVE}" == "1" ]]; then
    die "--ha-source is required in --non-interactive mode"
  fi
  if [[ ! -t 0 ]]; then
    die "--ha-source is required (no controlling terminal to prompt on)"
  fi
  read -r -p "Home Assistant host/subnet allowed to reach TCP 8787 (CIDR, e.g. 203.0.113.50/32): " HA_SOURCE_CIDR
fi

if [[ "${NETWORK_MODE}" == "static" && ( -z "${STATIC_IP_CIDR}" || -z "${STATIC_GATEWAY}" ) ]]; then
  if [[ "${BOOTSTRAP_NON_INTERACTIVE}" == "1" ]]; then
    die "--network static requires --ip and --gateway in --non-interactive mode"
  fi
  [[ -t 0 ]] || die "--network static requires --ip and --gateway (no controlling terminal to prompt on)"
  [[ -n "${STATIC_IP_CIDR}" ]] || read -r -p "Static IP address/prefix for the container (e.g. 203.0.113.110/24): " STATIC_IP_CIDR
  [[ -n "${STATIC_GATEWAY}" ]] || read -r -p "Gateway address: " STATIC_GATEWAY
fi

# ---------------------------------------------------------------------------
# Rollback/cleanup framework -- see the module docstrings for the exact
# semantics (PVE identity always rolled back on failure; CT preserved by
# default, destroyed only with --cleanup-on-failure; service/onboot never
# left exposed on any failure path). Cleanup is gated on "was this class
# of object ever attempted by this run" markers, not "did the specific
# creation call report success" -- a command can mutate PVE/CT state and
# still return a nonzero exit for an unrelated reason, so success-only
# ledger entries are not a reliable proxy for "nothing was created."
# ---------------------------------------------------------------------------

BOOTSTRAP_LEDGER="$(mktemp /tmp/hubinet-ops-bootstrap-ledger.XXXXXX)"
BOOTSTRAP_SECRET_FILES="$(mktemp /tmp/hubinet-ops-bootstrap-secrets.XXXXXX)"
chmod 0600 "${BOOTSTRAP_LEDGER}" "${BOOTSTRAP_SECRET_FILES}"

# shellcheck source=lib/bootstrap-preflight.sh
source "${BOOTSTRAP_SCRIPT_DIR}/lib/bootstrap-preflight.sh"
# shellcheck source=lib/bootstrap-container.sh
source "${BOOTSTRAP_SCRIPT_DIR}/lib/bootstrap-container.sh"
# shellcheck source=lib/bootstrap-identity.sh
source "${BOOTSTRAP_SCRIPT_DIR}/lib/bootstrap-identity.sh"
# shellcheck source=lib/bootstrap-deploy.sh
source "${BOOTSTRAP_SCRIPT_DIR}/lib/bootstrap-deploy.sh"
# shellcheck source=lib/bootstrap-host-control.sh
source "${BOOTSTRAP_SCRIPT_DIR}/lib/bootstrap-host-control.sh"
# shellcheck source=lib/bootstrap-update-boundaries.sh
source "${BOOTSTRAP_SCRIPT_DIR}/lib/bootstrap-update-boundaries.sh"
# shellcheck source=lib/bootstrap-firewall.sh
source "${BOOTSTRAP_SCRIPT_DIR}/lib/bootstrap-firewall.sh"
# shellcheck source=lib/bootstrap-finish.sh
source "${BOOTSTRAP_SCRIPT_DIR}/lib/bootstrap-finish.sh"

rollback_on_failure() {
  local exit_code="$1"

  log_warn "bootstrap failed (exit ${exit_code}) -- running cleanup"

  # Idempotent, unconditional once attempted -- closes the partial-success
  # window where `systemctl enable --now` mutates state but still returns
  # nonzero for an unrelated reason (see phase11_start_service).
  if ledger_has service-start-attempted "${VMID}"; then
    pct exec "${VMID}" -- systemctl disable --now hubinet-ops >/dev/null 2>&1 \
      || log_warn "could not stop/disable hubinet-ops inside container ${VMID} during cleanup (may never have been enabled -- not necessarily an error)"
  fi

  # Exact Hubinet-owned helper/key/authorization artifacts only. The
  # cleanup function filters by this run's unique marker and never
  # replaces or removes unrelated operator authorized_keys entries.
  rollback_update_boundaries
  rollback_host_control

  if ledger_has ct "${VMID}"; then
    pct set "${VMID}" --onboot 0 >/dev/null 2>&1 || true
  fi

  # PVE identity objects created in THIS run are rolled back automatically
  # -- but ONLY what this run can PROVE it owns, never merely "whatever
  # currently exists at our fixed name." A cluster-wide identity namespace
  # means an ambiguous mutate-then-error result (this run's own `pveum`
  # call reported failure, but the object now exists) cannot be assumed to
  # be this run's doing -- it could belong to a concurrent bootstrap run
  # (or any other creator) that won a race for the same fixed name. See
  # bootstrap-identity.sh's _user_object_owned_by_this_run /
  # _token_ownership_state / _role_object_owned_by_this_run for the exact
  # proof each object type supports (comment-embedded BOOTSTRAP_RUN_ID for
  # user/token; ledger-only for role, which PVE gives no comment field to
  # prove anything about). Gated on the single "pve-identity-attempted"
  # marker recorded in phase6 (after its own pre-existing-conflict checks,
  # before its first mutation) so a partial success is still evaluated for
  # cleanup; every delete below remains idempotent (`|| log_warn`) since
  # an OWNED object may still never have actually been created. Reverse
  # creation order.
  #
  # Eighth-pass corrective note (P1 finding, independent review): real
  # Proxmox's `pveum user delete` removes the deleted user's ENTIRE
  # configuration, including every token registered under it -- proven-
  # ours or not. An earlier version of this rollback proved the TOKEN
  # unowned/unproven and correctly skipped `pveum user token remove`, but
  # still unconditionally deleted the PARENT USER whenever the user
  # itself was proven owned -- indirectly destroying the very token this
  # same rollback had just logged as "preserved."
  #
  # Ninth-pass corrective note (P1/P2 findings, independent review):
  # closing that same-shaped bug a second time, two ways deeper.
  #   (1) `pveum acl delete / --users ... --roles ...` (removing the
  #       parent user's OWN acl grant) was still unconditional on
  #       user_owned alone -- but a privsep=1 token's effective
  #       permissions are the INTERSECTION of the owning user's
  #       permissions and the token's own permissions, so stripping the
  #       user's acl functionally disables an unproven/possibly-foreign
  #       token even while the token OBJECT itself is left untouched.
  #       An unproven token must block the WHOLE identity dependency
  #       chain: token, token acl, parent user, AND parent user acl.
  #   (2) parent user cleanup proceeded even when `pveum user token
  #       remove` itself failed (only logged, not gated on) -- a failed
  #       token removal must ALSO preserve the parent user and its acl,
  #       not merely be noted and continued past.
  #
  # Tenth-pass corrective note (P1 finding, independent review -- Codex):
  # even both of the above only ever proved the EXPECTED token
  # (r0-readonly) safe -- that is NOT sufficient authorization to touch
  # the parent user, because a different administrator or a concurrent
  # process could have registered an entirely different token under this
  # same fixed-name user at any point after this bootstrap created it.
  # `pveum user delete` would destroy that other token too, and removing
  # only the parent user's own ACL grant can functionally disable it
  # (same privsep=1 intersection reasoning as above). Parent cleanup now
  # additionally requires bootstrap-identity.sh's
  # _parent_user_child_token_state -- a FRESH, complete, authoritative
  # read of the user's ENTIRE token list, taken only after the expected
  # token is already safely handled -- to prove that list is empty.
  # expected_token_cleanup_safe below tracks only the first, narrower
  # proof (the expected token itself is absent, or owned and removed);
  # parent_user_cleanup_safe -- the single, explicit gate for BOTH the
  # parent user's acl removal and its final deletion -- becomes true
  # only when expected_token_cleanup_safe is true AND that fresh full
  # read proves no child token of any kind remains. This run has live
  # provenance only for its own expected token; it never attempts to
  # classify or remove any OTHER token it finds -- a residual token,
  # known or unknown, simply blocks parent cleanup.
  if ledger_has pve-identity-attempted "${PVE_USER:-hubinetops@pve}"; then
    local user_owned=0 role_owned=0 token_owned=0 expected_token_cleanup_safe=0 parent_user_cleanup_safe=0

    if _user_object_owned_by_this_run; then
      user_owned=1
    else
      # Sixth-pass corrective note: this generic message deliberately no
      # longer asserts a SPECIFIC reason ("no ledger record, and no
      # matching comment") -- _user_object_owned_by_this_run can return
      # "not owned" for several structurally different reasons (a
      # read-back command failure, malformed JSON, an unrecognized list
      # shape, a missing object, or a genuinely non-matching comment),
      # and it has ALREADY logged the specific one via its own log_warn
      # immediately above this. A real rollback run once logged a schema-
      # mismatch reason here while a manual read of the same list
      # immediately afterward showed a well-formed, valid array -- an
      # assertion here that duplicated (and could contradict) the
      # specific reason already logged would have been actively
      # misleading in exactly that situation.
      log_warn "PRESERVING PVE user '${PVE_USER}': this run could not prove live ownership of the current object (see the specific reason already logged above) -- it may belong to a concurrent bootstrap run or another creator racing for the same fixed name. Manual remediation: inspect 'pveum user list --output-format json' for '${PVE_USER}' and its comment field, confirm provenance yourself, then remove it only if you are certain it is safe: pveum user delete ${PVE_USER}"
    fi

    if _role_object_owned_by_this_run; then
      role_owned=1
    else
      log_warn "PRESERVING PVE role '${PVE_ROLE}': PVE roles carry no comment/provenance field, so this run can never re-verify at rollback time that the CURRENT role is still the one it created (even if this run's own ledger recorded a clean success earlier, the role could have been deleted and replaced by another actor since then) -- role rollback is deliberately never automatic. Manual remediation: inspect 'pveum role list' and 'pveum acl list' for '${PVE_ROLE}', confirm provenance yourself, then remove it only if you are certain it is safe: pveum role delete ${PVE_ROLE}"
    fi

    # Token ownership is checked independently too (not merely inherited
    # from user_owned) -- only queried once the user itself is proven
    # ours, since a token can only ever meaningfully exist under a user
    # this bootstrap itself created. Tri-state, not a boolean: OWNED
    # (safe to remove) / ABSENT (nothing to protect -- also safe to let
    # the parent user be cleaned up) / UNPROVEN (must block mutation of
    # the WHOLE identity dependency chain -- token, token acl, parent
    # user, AND parent user acl -- since even removing only the parent
    # user's acl can functionally disable an unproven/possibly-foreign
    # privsep=1 token, whose effective permissions are the intersection
    # of the user's and the token's own).
    if (( user_owned )); then
      local token_state
      token_state="$(_token_ownership_state)"
      case "${token_state}" in
        owned)
          token_owned=1
          ;;
        absent)
          # Nothing to preserve for the EXPECTED token -- but parent
          # cleanup itself still requires the fresh full-child-list
          # check below (a different token could still be present).
          expected_token_cleanup_safe=1
          ;;
        *)
          # Same reasoning as the user-preserve message above -- the
          # specific reason is already logged by _token_ownership_state
          # itself; this generic message must not assert (and potentially
          # contradict) a specific cause on top of it. parent_user_
          # cleanup_safe stays 0 -- an unproven token blocks cleanup of
          # its parent user (and that user's acl) too, not merely the
          # token itself.
          log_warn "PRESERVING PVE token '${PVE_FULL_TOKEN_ID}': this run could not prove live ownership of the current object (see the specific reason already logged above) -- manual remediation: inspect 'pveum user token list ${PVE_USER} --output-format json' for '${PVE_TOKEN_ID}' and its comment field, confirm provenance yourself, then remove it only if you are certain it is safe: pveum user token remove ${PVE_USER} ${PVE_TOKEN_ID}"
          ;;
      esac
    fi

    if (( token_owned )); then
      pveum acl delete / --tokens "${PVE_FULL_TOKEN_ID}" --roles "${PVE_ROLE}" >/dev/null 2>&1 \
        || log_warn "could not remove ACL grant for token ${PVE_FULL_TOKEN_ID} (may never have existed)"
      # Ninth-pass corrective note (P2 finding, independent review): the
      # token's own removal command's actual exit status now GATES
      # parent_user_cleanup_safe -- a failed removal here must preserve
      # the parent user (and its acl) too, never merely be logged and
      # continued past as if cleanup had completed.
      local token_remove_status
      pveum user token remove "${PVE_USER}" "${PVE_TOKEN_ID}" >/dev/null 2>&1 \
        && token_remove_status=0 || token_remove_status=$?
      if (( token_remove_status == 0 )); then
        expected_token_cleanup_safe=1
      else
        log_warn "could not remove token ${PVE_FULL_TOKEN_ID} (pveum exited ${token_remove_status}) -- this run's token cleanup is INCOMPLETE, not merely a cosmetic failure: PRESERVING parent user '${PVE_USER}' and its ACL grant too, since this token's removal could not be confirmed. Manual remediation: inspect 'pveum user token list ${PVE_USER} --output-format json' for '${PVE_TOKEN_ID}', remove it yourself once you have confirmed the cause (pveum user token remove ${PVE_USER} ${PVE_TOKEN_ID}), then the user (pveum user delete ${PVE_USER}) only if both are safe to remove."
      fi
    fi

    # P1 fix (tenth pass, independent review -- Codex): the expected
    # token being safely handled is necessary but NOT sufficient --
    # parent cleanup also requires a FRESH, complete proof that no OTHER
    # token remains registered under this user. Deliberately a fresh
    # read here, not a reuse of any earlier one: a child token could
    # have appeared in the window between the expected-token checks
    # above and this point.
    if (( user_owned )) && (( expected_token_cleanup_safe )); then
      local child_token_state
      child_token_state="$(_parent_user_child_token_state)"
      if [[ "${child_token_state}" == "empty" ]]; then
        parent_user_cleanup_safe=1
      else
        # The specific reason (command failure, schema-invalid, or a
        # genuine residual token) is already logged by
        # _parent_user_child_token_state itself.
        log_warn "PRESERVING PVE user '${PVE_USER}' and its ACL grant despite its expected token being safely handled: this run could not prove '${PVE_USER}' now has ZERO tokens registered under it (see the specific reason already logged above) -- deleting the user, or even just its ACL grant, could destroy or functionally disable a token this run has no provenance for. Manual remediation: inspect 'pveum user token list ${PVE_USER} --output-format json', confirm the provenance of every token yourself, remove each you are certain is safe, then the user (pveum user delete ${PVE_USER}) only once none remain."
      fi
    fi

    if (( user_owned )) && (( parent_user_cleanup_safe )); then
      pveum acl delete / --users "${PVE_USER}" --roles "${PVE_ROLE}" >/dev/null 2>&1 \
        || log_warn "could not remove ACL grant for user ${PVE_USER} (may never have existed)"
    fi

    if (( role_owned )); then
      pveum role delete "${PVE_ROLE}" >/dev/null 2>&1 \
        || log_warn "could not remove role ${PVE_ROLE} (may never have existed)"
    fi

    if (( user_owned )) && (( parent_user_cleanup_safe )); then
      pveum user delete "${PVE_USER}" >/dev/null 2>&1 \
        || log_warn "could not remove user ${PVE_USER} (may never have existed)"
    elif (( user_owned )); then
      # P1/P2 fix (ninth pass) + P1 fix (tenth pass, independent review):
      # do NOT delete a proven-owned user (or even just its acl grant,
      # above) whenever parent cleanup could not be proven safe -- the
      # expected token itself may be unproven or its removal may have
      # failed, OR the expected token may have been handled safely while
      # a fresh, complete read still could not prove the user's ENTIRE
      # token list is empty (unproven, or a genuine residual token this
      # run has no provenance for) -- since 'pveum user delete' removes
      # the user's entire configuration (destroying any token still
      # under it), and even a plain acl removal alone can functionally
      # disable a privsep=1 token that is still present.
      log_warn "PRESERVING PVE user '${PVE_USER}' and its ACL grant despite being proven owned by this run: this run could not prove parent cleanup is safe (see the specific reason already logged above) -- deleting the user, or even just its ACL grant, could functionally disable or destroy a token still registered under it as a side effect (a privsep=1 token's effective permissions are the intersection of the user's and the token's own, and this run only has live provenance for its own expected token '${PVE_FULL_TOKEN_ID}'). Manual remediation: inspect 'pveum user token list ${PVE_USER} --output-format json', confirm the provenance of every token yourself, remove each you are certain is safe, then the user (pveum user delete ${PVE_USER}) only once none remain."
    fi
  fi

  # The CT itself is preserved by default for forensic diagnosis --
  # destroyed only if the operator explicitly opted in.
  if ledger_has ct "${VMID}"; then
    if [[ "${BOOTSTRAP_CLEANUP_ON_FAILURE}" == "1" ]]; then
      log_warn "--cleanup-on-failure set -- destroying container ${VMID}"
      pct stop "${VMID}" >/dev/null 2>&1 || true
      pct destroy "${VMID}" >/dev/null 2>&1 \
        || log_warn "could not destroy container ${VMID} during cleanup -- manual removal required (pct stop ${VMID} && pct destroy ${VMID})"
    else
      log_warn "preserving container ${VMID} for forensic diagnosis. It is NOT started for boot (onboot=0) and its Hubinet Ops service is stopped/disabled if it was ever started. Re-run with --cleanup-on-failure to destroy it automatically, or remove it yourself: pct stop ${VMID} && pct destroy ${VMID}"
    fi
  fi

  cleanup_secret_files
  rm -f -- "${BOOTSTRAP_LEDGER}"
  log_warn "bootstrap did not complete -- exiting ${exit_code}"
}

_bootstrap_exit_trap() {
  local exit_code=$?
  trap - EXIT
  if (( exit_code == 0 )); then
    cleanup_secret_files
    rm -f -- "${BOOTSTRAP_LEDGER}"
  else
    rollback_on_failure "${exit_code}"
  fi
  exit "${exit_code}"
}

trap _bootstrap_exit_trap EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# ---------------------------------------------------------------------------
# Orchestration -- fixed phase order, never reordered. Everything before
# confirm_or_abort is read-only planning; nothing after phase1/phase2/
# _plan_source_commit mutates PVE or CT state until the operator has
# confirmed the printed plan.
# ---------------------------------------------------------------------------

phase1_preflight
phase2_plan_template
phase2c_plan_host_control
phase2d_plan_update_boundaries
_plan_source_commit

log_info "Plan: create VMID ${VMID}$( [[ "${VMID_EXPLICIT}" == "0" ]] && printf ' (auto-detected next-free)' ) (${HOSTNAME_}) from template [${TEMPLATE_PLAN_NOTE}] on storage ${STORAGE}, bridge ${BRIDGE}, network ${NETWORK_MODE}; PVE endpoint ${PVE_ENDPOINT}; source commit ${SOURCE_HEAD_SHA}; HA source ${HA_SOURCE_CIDR} -> TCP 8787 only; package scans every ${PACKAGE_SCAN_INTERVAL_SECONDS}s through one pinned-key, forced-command SSH boundary; operator-triggered package updates through five further dedicated-key forced-command boundaries (snapshot, plan simulation, mutation, rollback, health) -- no update ever starts without an explicit operator action."
confirm_or_abort "Proceed with this plan?"

phase2b_provision_template
phase3_create_container
phase4_debian13_compat
phase5_boot_and_discover_ip
phase6_pve_identity
phase7_tls_trust
phase8_deploy_source
phase8b_provision_tooling
phase8c_provision_host_control
phase8d_provision_update_boundaries
phase9_generate_config
phase10_firewall
phase11_start_service
phase12_acceptance
phase13_finish

exit 0
