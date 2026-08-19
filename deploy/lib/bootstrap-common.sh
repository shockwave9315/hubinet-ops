#!/usr/bin/env bash
# Hubinet Ops 0.5 R0 Proxmox bootstrap -- shared helpers.
#
# Sourced by deploy/bootstrap-proxmox-0.5.sh and the other deploy/lib/
# bootstrap-*.sh phase modules. Never executed directly. Assumes the
# caller has already set `set -Eeuo pipefail`.
#
# Deliberately no `set -x` anywhere in this file or any phase module:
# xtrace would echo full command lines, including PVE token secrets
# passed as arguments/heredocs during identity/config generation. Logging
# below is explicit and secret-redacted instead.

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_BOOTSTRAP_LOG_PREFIX="hubinet-ops-bootstrap"

log_info() { printf '[%s] %s\n' "${_BOOTSTRAP_LOG_PREFIX}" "$*" >&2; }
log_warn() { printf '[%s] WARN: %s\n' "${_BOOTSTRAP_LOG_PREFIX}" "$*" >&2; }
log_phase() { printf '\n[%s] === %s ===\n' "${_BOOTSTRAP_LOG_PREFIX}" "$*" >&2; }
log_pass() { printf '[%s] PASS: %s\n' "${_BOOTSTRAP_LOG_PREFIX}" "$*" >&2; }

# die: log a FAIL line and exit non-zero. Never called with secret material
# in the message -- callers are responsible for redacting before calling.
die() {
  printf '[%s] FAIL: %s\n' "${_BOOTSTRAP_LOG_PREFIX}" "$*" >&2
  exit 1
}

# ---------------------------------------------------------------------------
# Resource ledger -- tracks exactly what THIS run created, so rollback never
# touches a pre-existing operator resource. Appended to a plain file (not a
# bash array) so it survives correctly across the ERR/EXIT trap without any
# subshell-visibility surprises.
# ---------------------------------------------------------------------------

# BOOTSTRAP_LEDGER must be set by the entrypoint before sourcing this file
# (a mktemp'd file, cleaned up by the entrypoint's own trap after rollback
# has read it). Format: one "<kind> <id>" pair per line, append-only,
# earliest-created-last-removed order preserved by construction.
ledger_record() {
  local kind="$1" id="$2"
  printf '%s %s\n' "${kind}" "${id}" >>"${BOOTSTRAP_LEDGER}"
}

# ledger_has: true if this run itself created the given kind/id pair.
# Rollback and idempotency checks use this to distinguish "this run made
# it" from "an operator resource happened to already exist" -- never the
# other way around.
ledger_has() {
  local kind="$1" id="$2"
  [[ -f "${BOOTSTRAP_LEDGER}" ]] || return 1
  grep -qxF "${kind} ${id}" "${BOOTSTRAP_LEDGER}"
}

# ---------------------------------------------------------------------------
# Secret-safe temp files
# ---------------------------------------------------------------------------

# BOOTSTRAP_SECRET_FILES is a plain file (one path per line) of every
# secret-bearing temp file this run has created, so the exit trap can wipe
# them regardless of which phase failed. Set by the entrypoint before
# sourcing this file, same lifecycle as BOOTSTRAP_LEDGER.
secret_tmpfile() {
  local template="$1"
  local path
  local old_umask
  old_umask="$(umask)"
  umask 0177
  path="$(mktemp "${template}")"
  umask "${old_umask}"
  printf '%s\n' "${path}" >>"${BOOTSTRAP_SECRET_FILES}"
  printf '%s' "${path}"
}

cleanup_secret_files() {
  [[ -f "${BOOTSTRAP_SECRET_FILES:-}" ]] || return 0
  while IFS= read -r path; do
    [[ -n "${path}" && -e "${path}" ]] || continue
    # Best-effort overwrite before unlink; not a substitute for full disk
    # encryption, but avoids leaving plaintext secret bytes trivially
    # recoverable from an unallocated block on the PVE host's own disk.
    : >"${path}" 2>/dev/null || true
    rm -f -- "${path}"
  done <"${BOOTSTRAP_SECRET_FILES}"
  rm -f -- "${BOOTSTRAP_SECRET_FILES}"
}

# ---------------------------------------------------------------------------
# Command execution -- never eval, never string-built shell, always an
# argv array passed through "$@".
# ---------------------------------------------------------------------------

# require_command: fail closed if a required external command is missing.
require_command() {
  local cmd="$1" purpose="$2"
  command -v "${cmd}" >/dev/null 2>&1 || die "required command '${cmd}' not found (${purpose})"
}

# run_logged: execute argv, logging the command (space-joined, quoted for
# readability) at info level first. Only ever call this with a command line
# that contains no secret material -- PVE token creation/use goes through
# run_logged_redacted instead.
run_logged() {
  log_info "+ $*"
  "$@"
}

# run_logged_redacted: execute argv, but log a caller-supplied redacted
# description instead of the real argv (which may contain a secret in one
# position). The real argv is still executed exactly as given -- quoting is
# preserved because bash never re-splits "$@".
run_logged_redacted() {
  local redacted_description="$1"
  shift
  log_info "+ ${redacted_description}"
  "$@"
}

# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

is_positive_int() {
  [[ "$1" =~ ^[1-9][0-9]*$ ]]
}

# is_valid_vmid: Proxmox VMIDs are integers in [100, 999999999].
is_valid_vmid() {
  is_positive_int "$1" && (( $1 >= 100 && $1 <= 999999999 ))
}

# is_valid_ipv4_cidr: "a.b.c.d/n", each octet 0-255, prefix 0-32. Used for
# --ha-source and static IP validation. Deliberately conservative (rejects
# IPv6, hostnames) -- this bootstrap targets the documented IPv4 firewall
# model in deploy/README-0.5-firewall.md; IPv6 is out of scope for this
# wave, not silently mishandled.
is_valid_ipv4_cidr() {
  local value="$1"
  [[ "${value}" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})/([0-9]{1,2})$ ]] || return 1
  local o1="${BASH_REMATCH[1]}" o2="${BASH_REMATCH[2]}" o3="${BASH_REMATCH[3]}" o4="${BASH_REMATCH[4]}" prefix="${BASH_REMATCH[5]}"
  local octet
  for octet in "${o1}" "${o2}" "${o3}" "${o4}"; do
    (( octet <= 255 )) || return 1
  done
  (( prefix <= 32 )) || return 1
  return 0
}

is_valid_ipv4() {
  local value="$1"
  [[ "${value}" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]] || return 1
  local o1="${BASH_REMATCH[1]}" o2="${BASH_REMATCH[2]}" o3="${BASH_REMATCH[3]}" o4="${BASH_REMATCH[4]}"
  local octet
  for octet in "${o1}" "${o2}" "${o3}" "${o4}"; do
    (( octet <= 255 )) || return 1
  done
  return 0
}

# is_valid_https_url: minimal, conservative check -- the authoritative
# validation is app.inventory.canonicalization.canonicalize_transport_locator
# (invoked indirectly when the generated inventory.yaml is first loaded by
# the R0 runtime); this is only an early, human-friendly fail-closed guard
# during bootstrap itself.
is_valid_https_url() {
  [[ "$1" =~ ^https://[A-Za-z0-9.-]+(:[0-9]+)?/?$ ]]
}

# confirm_or_abort: interactive-only confirmation. In --non-interactive
# mode, with --yes, or with no controlling terminal, this is a no-op
# (proceeds) -- every safety-relevant precondition is already enforced by
# phase1_preflight independently of this prompt, so skipping it never
# bypasses a real gate; it only skips an extra human "are you sure".
confirm_or_abort() {
  local prompt="$1"
  if [[ "${BOOTSTRAP_ASSUME_YES:-0}" == "1" || "${BOOTSTRAP_NON_INTERACTIVE:-0}" == "1" || ! -t 0 ]]; then
    return 0
  fi
  local reply
  read -r -p "${prompt} [y/N] " reply
  case "${reply}" in
    y|Y|yes|YES) return 0 ;;
    *) die "aborted by operator" ;;
  esac
}
