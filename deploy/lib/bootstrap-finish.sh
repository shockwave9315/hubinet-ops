#!/usr/bin/env bash
# Phase 11 -- start the service, only after firewall + config + credentials
#             + TLS are all in place.
# Phase 12 -- acceptance checks, bounded timeouts throughout.
# Phase 13 -- CT onboot enablement (last) + operator success summary.

phase11_start_service() {
  log_phase "Phase 11: start hubinet-ops"

  run_logged pct exec "${VMID}" -- systemctl enable --now hubinet-ops \
    || die "failed to enable/start hubinet-ops inside container ${VMID}"
  ledger_record service-started "${VMID}"

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
  _accept_backend_health
  _accept_backend_snapshot

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
  # Re-derives the same two structural facts phase 10 already verified,
  # as a final pre-go-live recheck rather than trusting an earlier phase's
  # result unconditionally.
  local ruleset
  ruleset="$(pct exec "${VMID}" -- nft list ruleset 2>/dev/null)" || die "acceptance failed: could not read nftables ruleset"
  [[ "${ruleset}" == *"ip saddr ${HA_SOURCE_CIDR} tcp dport 8787 accept"* ]] \
    || die "acceptance failed: HA source allow rule missing from active ruleset"
  [[ "${ruleset}" == *'meta skuid "hubinetops" drop'* ]] \
    || die "acceptance failed: hubinetops egress default-deny missing from active ruleset"
}

_accept_backend_health() {
  local waited=0
  local interval=1
  local body=""
  while (( waited < BOOTSTRAP_SERVICE_TIMEOUT_SECONDS )); do
    body="$(pct exec "${VMID}" -- curl -fsS "http://127.0.0.1:8787/r0/v1/health" 2>/dev/null || true)"
    [[ "${body}" == *'"status"'*'"ok"'* ]] && return 0
    sleep "${interval}"
    waited=$(( waited + interval ))
  done
  die "acceptance failed: GET /r0/v1/health did not return a healthy body within ${BOOTSTRAP_SERVICE_TIMEOUT_SECONDS}s (last response: ${body:-<empty>})"
}

_accept_backend_snapshot() {
  local body
  body="$(pct exec "${VMID}" -- curl -fsS -H "Authorization: Bearer ${R0_API_BEARER_TOKEN}" "http://127.0.0.1:8787/r0/v1/snapshot" 2>/dev/null || true)" \
    || true
  [[ -n "${body}" ]] || die "acceptance failed: GET /r0/v1/snapshot returned no body"
  [[ "${body}" == *'"sources"'* ]] \
    || die "acceptance failed: /r0/v1/snapshot response did not contain a sources[] field"
  # No static VMID list is ever asserted here by design -- resources[] may
  # legitimately be empty on the very first cycle before discovery
  # completes; that is a healthy, expected state, not a failure.
}

phase13_finish() {
  log_phase "Phase 13: finalize"

  run_logged pct set "${VMID}" --onboot 1 \
    || die "failed to enable onboot for container ${VMID} after successful acceptance"

  cat <<SUMMARY

Hubinet Ops 0.5 R0 bootstrap: PASS

VMID:               ${VMID}
CT address:         ${CT_IP}
PVE endpoint:        ${PVE_ENDPOINT}
PVE credential:      ${PVE_FULL_TOKEN_ID}
Permission profile:  Sys.Audit + VM.Audit only
Firewall:            PASS
Backend:             PASS
Discovery:           PASS (dynamic; no static VMID list configured)
Mutation authority:  NONE
CT onboot:           enabled

Next:
  Add the native Hubinet Ops integration in Home Assistant.
    Base URL:   http://${CT_IP}:8787
    Bearer token: stored in /etc/hubinet-ops/agent.env inside CT ${VMID} (not printed here)

SUMMARY
}
