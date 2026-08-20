#!/usr/bin/env bash
# Phase 11 -- start the service, only after firewall + config + credentials
#             + TLS are all in place.
# Phase 12 -- acceptance checks, bounded timeouts throughout.
# Phase 13 -- CT onboot enablement (last) + operator success summary.

CT_ACCEPT_SCRIPT_CT="/tmp/hubinet-ops-bootstrap-accept.py"

phase11_start_service() {
  log_phase "Phase 11: start hubinet-ops"

  # Recorded before the mutating call: if `systemctl enable --now` mutates
  # state (e.g. persists the enable symlink) but still reports a nonzero
  # exit for an unrelated reason, rollback still knows to attempt an
  # idempotent disable -- see rollback_on_failure in
  # bootstrap-proxmox-0.5.sh, which gates service cleanup on this marker
  # rather than a success-only "service-started" entry.
  ledger_record service-start-attempted "${VMID}"

  run_logged pct exec "${VMID}" -- systemctl enable --now hubinet-ops \
    || die "failed to enable/start hubinet-ops inside container ${VMID}"

  local waited=0
  local interval=1
  local state=""
  while (( waited < BOOTSTRAP_SERVICE_TIMEOUT_SECONDS )); do
    state="$(pct exec "${VMID}" -- systemctl is-active hubinet-ops 2>/dev/null || true)"
    [[ "${state}" == "active" ]] && break
    sleep "${interval}"
    waited=$(( waited + interval ))
  done
  [[ "${state}" == "active" ]] \
    || die "hubinet-ops did not reach 'active' within ${BOOTSTRAP_SERVICE_TIMEOUT_SECONDS}s (last state: ${state:-unknown}) -- check 'pct exec ${VMID} -- journalctl -u hubinet-ops -n 100 --no-pager'"

  log_pass "hubinet-ops active"
}

phase12_acceptance() {
  log_phase "Phase 12: acceptance"

  _accept_systemd_health
  _accept_service_state
  _accept_legacy_absence
  _accept_listener
  _accept_firewall_summary_recheck
  _accept_process_health
  _accept_discovery

  log_pass "acceptance"
}

_accept_systemd_health() {
  local status
  status="$(pct exec "${VMID}" -- systemctl is-system-running 2>/dev/null || true)"
  [[ "${status}" == "running" || "${status}" == "degraded" ]] \
    || die "acceptance failed: systemctl is-system-running reported '${status}' inside container ${VMID}"
  # 'degraded' alone is not automatically fatal (some unrelated unit could
  # be legitimately inactive), but zero *failed* units is mandatory.
  local failed
  failed="$(pct exec "${VMID}" -- systemctl --failed --no-legend 2>/dev/null || true)"
  [[ -z "${failed}" ]] \
    || die "acceptance failed: container ${VMID} has failed systemd units after boot:"$'\n'"${failed}"
}

_accept_service_state() {
  local active enabled
  active="$(pct exec "${VMID}" -- systemctl is-active hubinet-ops 2>/dev/null || true)"
  [[ "${active}" == "active" ]] || die "acceptance failed: hubinet-ops is not active (${active})"
  enabled="$(pct exec "${VMID}" -- systemctl is-enabled hubinet-ops 2>/dev/null || true)"
  [[ "${enabled}" == "enabled" ]] || die "acceptance failed: hubinet-ops is not enabled (${enabled})"
}

_accept_legacy_absence() {
  pct exec "${VMID}" -- test -e /var/lib/hubinet-ops/ops.db \
    && die "acceptance failed: legacy /var/lib/hubinet-ops/ops.db exists inside container ${VMID} -- this must never have been present before install"
  pct exec "${VMID}" -- systemctl status hubinet-ops-hostd >/dev/null 2>&1 \
    && die "acceptance failed: legacy hostd unit is present inside container ${VMID}"
  pct exec "${VMID}" -- ss -ltnp 2>/dev/null | grep -q ':8741 ' \
    && die "acceptance failed: legacy hostd port 8741 has a listener inside container ${VMID}"
  return 0
}

_accept_listener() {
  local waited=0
  local interval=1
  while (( waited < BOOTSTRAP_SERVICE_TIMEOUT_SECONDS )); do
    if pct exec "${VMID}" -- ss -ltn 2>/dev/null | grep -q ':8787 '; then
      return 0
    fi
    sleep "${interval}"
    waited=$(( waited + interval ))
  done
  die "acceptance failed: no TCP 8787 listener found inside container ${VMID} within ${BOOTSTRAP_SERVICE_TIMEOUT_SECONDS}s"
}

_accept_firewall_summary_recheck() {
  # A final pre-go-live recheck: re-runs the SAME exact-content/order
  # verification phase 10 already performed, rather than trusting an
  # earlier phase's result unconditionally.
  _verify_firewall_active
}

# _accept_process_health: a quick, unauthenticated liveness probe (no
# secret involved) before attempting the heavier, authenticated discovery
# check below -- gives a clearer error message if the HTTP server itself
# never came up at all.
_accept_process_health() {
  local waited=0
  local interval=1
  local body=""
  while (( waited < BOOTSTRAP_SERVICE_TIMEOUT_SECONDS )); do
    body="$(pct exec "${VMID}" -- curl -fsS "http://127.0.0.1:8787/r0/v1/health" 2>/dev/null || true)"
    [[ -n "${body}" ]] && return 0
    sleep "${interval}"
    waited=$(( waited + interval ))
  done
  die "acceptance failed: GET /r0/v1/health returned no response within ${BOOTSTRAP_SERVICE_TIMEOUT_SECONDS}s"
}

# _accept_discovery: the real acceptance gate (Mandatory Fix 3). Pushes
# and runs deploy/lib/hubinet-ops-bootstrap-accept.py INSIDE the container
# -- it authenticates with GET /r0/v1/backend (validating a real
# backend_instance_id) and then polls GET /r0/v1/snapshot, parsing the
# response as real JSON (never a substring match), until the configured
# source reports SourceHealth.HEALTHY with matching provenance, or the
# bounded --discovery-timeout elapses. A source stuck in
# not_yet_observed/configuration_error/source_unavailable/degraded (past
# the timeout)/an unreachable endpoint never yields PASS here, and
# `onboot=1` in phase13 is reached only if this function returns 0.
#
# The R0 API bearer token is read by that script directly from
# /etc/hubinet-ops/agent.env inside the container -- it is never passed to
# `pct exec`, curl, or this bash script as an argument or environment
# variable.
_accept_discovery() {
  run_logged pct push "${VMID}" "${BOOTSTRAP_SCRIPT_DIR}/lib/hubinet-ops-bootstrap-accept.py" "${CT_ACCEPT_SCRIPT_CT}" \
    || die "failed to push discovery-acceptance script into container ${VMID}"

  # `... && status=0 || status=$?` rather than a plain assignment: under
  # `set -e`, a bare `output="$(cmd)"` where cmd exits nonzero makes the
  # ASSIGNMENT ITSELF "fail," which triggers errexit and aborts the whole
  # script right here -- silently skipping every line below, including
  # the informative die() message this function exists to produce. Every
  # negative discovery-acceptance scenario (the ones that matter most)
  # would otherwise terminate with only a bare, uninformative exit code.
  local output status
  output="$(pct exec "${VMID}" -- python3 "${CT_ACCEPT_SCRIPT_CT}" "${HA_DISPLAY_NAME}" "${BOOTSTRAP_DISCOVERY_TIMEOUT_SECONDS}" 2>&1)" && status=0 || status=$?

  pct exec "${VMID}" -- rm -f "${CT_ACCEPT_SCRIPT_CT}" >/dev/null 2>&1 \
    || log_warn "could not remove ${CT_ACCEPT_SCRIPT_CT} inside the container (non-fatal)"

  printf '%s\n' "${output}" | while IFS= read -r line; do log_info "discovery: ${line}"; done

  local last_line
  last_line="$(printf '%s\n' "${output}" | tail -n1)"
  [[ "${status}" -eq 0 && "${last_line}" == PASS* ]] \
    || die "acceptance failed: discovery did not reach a healthy, provenance-verified state within ${BOOTSTRAP_DISCOVERY_TIMEOUT_SECONDS}s (${last_line:-no output})"

  DISCOVERY_ACCEPTANCE_SUMMARY="${last_line}"
}

phase13_finish() {
  log_phase "Phase 13: finalize"

  run_logged pct set "${VMID}" --onboot 1 \
    || die "failed to enable onboot for container ${VMID} after successful acceptance"

  cat <<SUMMARY

Hubinet Ops 0.5 R0 bootstrap: PASS

VMID:                ${VMID}
CT address:          ${CT_IP}
PVE endpoint:         ${PVE_ENDPOINT}
Source commit:        ${SOURCE_HEAD_SHA}
PVE credential:       ${PVE_FULL_TOKEN_ID}
Permission profile:   exactly ${PVE_REQUIRED_PRIVS}
Firewall:             PASS (exact rule content/order verified)
Discovery:            PASS (${DISCOVERY_ACCEPTANCE_SUMMARY})
Mutation authority:   NONE
CT onboot:            enabled

Next:
  Add the native Hubinet Ops integration in Home Assistant.
    Base URL:   http://${CT_IP}:8787
    Bearer token: stored in /etc/hubinet-ops/agent.env inside CT ${VMID} (not printed here)

SUMMARY
}
