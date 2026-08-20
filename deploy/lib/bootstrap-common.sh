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
#
# Secret-handling rule enforced throughout this file and every phase
# module: a secret value (the PVE API token, the R0 API bearer token, the
# raw "pveum user token add" JSON containing the token) is never passed as
# a literal command-line argument to any process -- not to printf, not to
# jq/python3/awk, not to curl. Every helper below that needs to read
# secret-bearing content takes a file path (never secret) and reads the
# content internally from that file/stdin. Corrective-pass note: an
# earlier version of this file passed secret JSON via `printf '%s'
# "${json}"` and `awk -v token="${var}"`, which put the secret literally
# into those processes' own argv (visible via /proc/<pid>/cmdline for the
# duration of the call) -- fixed here structurally, not by moving the
# secret into an environment variable instead (which only changes, not
# closes, the same exposure surface).

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
# other way around. Corrective-pass note: rollback no longer uses this as
# the ONLY gate for whether to attempt an idempotent cleanup action (see
# bootstrap-proxmox-0.5.sh's rollback_on_failure) -- a command can mutate
# state and still return non-zero, so "ledger_record happened" is not a
# reliable proxy for "nothing was created." ledger_has(ct/pve-user/
# pve-role) remains the sole authority for whether a resource was ever
# reachable-created by THIS run at all (never touch anything for which
# this returns false), but once true, cleanup attempts every dependent
# sub-step idempotently rather than gating each one on its own separate
# success-only ledger entry.
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
# JSON helpers -- every function below takes a file path, never secret
# content as an argv string. `python3` is a hard preflight requirement
# (phase1_preflight) specifically so this repository never has to fall
# back to a lexical regex "parser" for security-verification-relevant
# JSON (the effective-PVE-permission exact-set check in particular); `jq`
# is used opportunistically first when present because it's faster/
# simpler, but is never required.
# ---------------------------------------------------------------------------

# _json_string_value <file> <key>: top-level string/number field value, or
# empty string if absent/null.
_json_string_value() {
  local file="$1" key="$2"
  # `tr -d '\r'` defensively strips any stray carriage return (see
  # _json_truthy_keys_sorted above) -- load-bearing here specifically
  # because this function's output is written directly into
  # PVE_TOKEN_SECRET_FILE, and a trailing '\r' left in that file would
  # silently corrupt the credential written into agent.env later
  # (awk's `getline` strips a trailing '\n' but not a preceding '\r').
  if command -v jq >/dev/null 2>&1; then
    jq -r --arg k "${key}" '.[$k] // empty' "${file}" 2>/dev/null | tr -d '\r'
    return
  fi
  python3 -c '
import json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    data = json.load(fh)
value = data.get(sys.argv[2])
if value is not None:
    print(value)
' "${file}" "${key}" 2>/dev/null | tr -d '\r'
}

# _json_truthy_keys_sorted <file>: sorted, newline-separated list of every
# top-level key whose value is truthy (1/true) in a flat JSON object --
# used for the exact-set PVE effective-permission proof (Mandatory Fix 2).
# Deliberately NOT a substring/blacklist check: this returns the complete
# actual key set so callers can assert set equality against exactly what
# is expected, catching any unexpected extra privilege regardless of its
# name.
_json_truthy_keys_sorted() {
  local file="$1"
  # `tr -d '\r'` defensively strips any stray carriage return a given
  # jq/python3 build might emit (observed on a Windows text-mode python
  # during development) -- harmless on a normal POSIX PVE host, where
  # neither jq nor python3 ever emits '\r', but load-bearing for exact
  # line-for-line set-equality comparison against this function's output
  # wherever it runs.
  if command -v jq >/dev/null 2>&1; then
    jq -r 'to_entries | map(select(.value == true or .value == 1)) | .[].key' "${file}" 2>/dev/null | tr -d '\r' | LC_ALL=C sort
    return
  fi
  python3 -c '
import json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    data = json.load(fh)
keys = sorted(k for k, v in data.items() if v in (1, True, "1"))
for key in keys:
    print(key)
' "${file}" 2>/dev/null | tr -d '\r' | LC_ALL=C sort
}

# _json_list_is_valid <file>: true if `file` parses as a valid JSON array
# (regardless of whether it is empty). Third-pass corrective note: earlier
# callers of _json_list_field_equals (phase6's pre-existing-identity-
# conflict checks) treated "command failed" and "produced no valid JSON"
# identically to "object legitimately absent, safe to proceed" (via a bare
# `... || true` around the listing command) -- a transient `pveum` error,
# a rejected --output-format flag, or truncated output would then be
# silently read as proof nothing conflicts, and the run would proceed to
# create a PVE identity anyway. This helper lets a caller require a
# genuinely successful, parseable read before trusting an "absent" result
# from _json_list_field_equals -- command failure or malformed JSON must
# always be a hard stop for a security-relevant conflict check, never a
# fall-through to "absent."
_json_list_is_valid() {
  local file="$1"
  [[ -s "${file}" ]] || return 1
  if command -v jq >/dev/null 2>&1; then
    jq -e 'type == "array"' "${file}" >/dev/null 2>&1
    return
  fi
  python3 -c '
import json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    data = json.load(fh)
sys.exit(0 if isinstance(data, list) else 1)
' "${file}" 2>/dev/null
}

# _json_list_field_equals <file> <field> <value>: true if the JSON array at
# `file` contains at least one object whose `field` equals `value`.
_json_list_field_equals() {
  local file="$1" field="$2" value="$3"
  if command -v jq >/dev/null 2>&1; then
    jq -e --arg f "${field}" --arg v "${value}" 'any(.[]; .[$f] == $v)' "${file}" >/dev/null 2>&1
    return
  fi
  python3 -c '
import json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    data = json.load(fh)
field, value = sys.argv[2], sys.argv[3]
sys.exit(0 if any(row.get(field) == value for row in data) else 1)
' "${file}" "${field}" "${value}" 2>/dev/null
}

# _json_list_field_value <file> <match_field> <match_value> <target_field>:
# prints `target_field` of the first array element whose `match_field`
# equals `match_value`; empty (and a nonzero exit) if no such element
# exists or `target_field` is null/absent. Used for PVE ownership-
# provenance checks (bootstrap-identity.sh) -- e.g. reading back an
# existing user's/token's own `comment` field to check whether it carries
# THIS run's random run-id marker.
_json_list_field_value() {
  local file="$1" match_field="$2" match_value="$3" target_field="$4"
  if command -v jq >/dev/null 2>&1; then
    jq -r --arg mf "${match_field}" --arg mv "${match_value}" --arg tf "${target_field}" \
      'map(select(.[$mf] == $mv)) | .[0][$tf] // empty' "${file}" 2>/dev/null | tr -d '\r'
    return
  fi
  python3 -c '
import json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    data = json.load(fh)
match_field, match_value, target_field = sys.argv[2], sys.argv[3], sys.argv[4]
for row in data:
    if row.get(match_field) == match_value:
        value = row.get(target_field)
        if value is not None:
            print(value)
        break
' "${file}" "${match_field}" "${match_value}" "${target_field}" 2>/dev/null | tr -d '\r'
}

# _generate_run_id: a per-invocation random identifier embedded into PVE
# object comments (where PVE supports a comment field) so a later rollback
# can PROVE, via read-only inspection, that a given object was actually
# created by THIS run -- rather than assuming ownership merely because the
# object's name matches this bootstrap's fixed naming scheme. See
# bootstrap-identity.sh's ownership-proof functions and
# bootstrap-proxmox-0.5.sh's rollback_on_failure for how this closes a
# real TOCTOU race between two concurrent bootstrap runs (or an unrelated
# concurrent creator) contending for the same fixed PVE user/role/token
# name. 16 random bytes hex-encoded via /dev/urandom (always present on a
# real Linux PVE host); a defensive, still-unpredictable-enough fallback
# if /dev/urandom is somehow unavailable (never expected in practice).
_generate_run_id() {
  local id
  id="$(od -An -tx1 -N16 /dev/urandom 2>/dev/null | tr -d ' \n')"
  if [[ -z "${id}" ]]; then
    id="$(date +%s%N 2>/dev/null || date +%s)-$$-${RANDOM}${RANDOM}"
  fi
  printf '%s' "${id}"
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

# confirm_or_abort: skipped only by an explicit --yes/--non-interactive.
# Deliberately does NOT special-case "stdin is not a tty" as "skip the
# prompt and proceed" -- `read -r -p` works correctly against a piped
# answer (e.g. `echo n | bootstrap-proxmox-0.5.sh`), and treating a
# non-tty stdin as an automatic "yes" would silently bypass the very
# confirmation gate this bootstrap's "no mutation before confirmation"
# invariant depends on. If stdin is genuinely closed/unreadable (`read`
# itself fails, e.g. redirected from /dev/null), that is fail-closed
# (die), never fail-open.
confirm_or_abort() {
  local prompt="$1"
  if [[ "${BOOTSTRAP_ASSUME_YES:-0}" == "1" || "${BOOTSTRAP_NON_INTERACTIVE:-0}" == "1" ]]; then
    return 0
  fi
  local reply
  read -r -p "${prompt} [y/N] " reply \
    || die "no confirmation could be read from stdin (not running with --non-interactive/--yes, and stdin is closed or unreadable) -- pass --non-interactive --yes for unattended use, or ensure a real answer can be read on stdin"
  case "${reply}" in
    y|Y|yes|YES) return 0 ;;
    *) die "aborted by operator" ;;
  esac
}
