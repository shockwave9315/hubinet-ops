#!/usr/bin/env bash
# Phase 10 -- mandatory R0 firewall boundary, automated per
# deploy/README-0.5-firewall.md's nftables model: egress confined to the
# `hubinetops` service user (meta skuid), ingress restricted to the
# configured HA host/subnet on TCP 8787 only.

CT_NFT_CONF_PATH="/etc/nftables.conf"

phase10_firewall() {
  log_phase "Phase 10: firewall"

  local pve_host pve_host_rule_target
  pve_host="$(_endpoint_host "${PVE_ENDPOINT}")"
  pve_host_rule_target="${pve_host}"

  local dns_rule=""
  if ! is_valid_ipv4 "${pve_host}"; then
    if [[ -z "${DNS_RESOLVER_IP}" ]]; then
      die "source.pve_endpoint ('${PVE_ENDPOINT}') uses a hostname, not a literal IP -- pass --dns-resolver <your-internal-resolver-ip> so egress can be scoped narrowly, or reconfigure --pve-endpoint with a literal IP and omit --dns-resolver entirely"
    fi
    is_valid_ipv4 "${DNS_RESOLVER_IP}" || die "--dns-resolver '${DNS_RESOLVER_IP}' is not a valid IPv4 address"
    dns_rule="    meta skuid \"hubinetops\" ip daddr ${DNS_RESOLVER_IP} udp dport 53 accept"
    log_info "PVE endpoint uses a hostname -- adding a DNS egress rule scoped to resolver ${DNS_RESOLVER_IP} only"
  fi

  local ruleset_tmp
  ruleset_tmp="$(secret_tmpfile "/tmp/hubinet-ops-bootstrap-nft.XXXXXX.conf")"
  # Not secret, but the same restrictive-tempfile helper is reused for
  # convenience/guaranteed cleanup.
  {
    printf '#!/usr/sbin/nft -f\n'
    printf 'table inet hubinet_ops_r0 {\n'
    printf '  chain input {\n'
    printf '    type filter hook input priority 0; policy accept;\n'
    printf '    ip saddr %s tcp dport 8787 accept\n' "${HA_SOURCE_CIDR}"
    printf '    tcp dport 8787 drop\n'
    printf '  }\n'
    printf '  chain output {\n'
    printf '    type filter hook output priority 0; policy accept;\n'
    printf '    meta skuid "hubinetops" ip daddr %s tcp dport 8006 accept\n' "${pve_host_rule_target}"
    if [[ -n "${dns_rule}" ]]; then
      printf '%s\n' "${dns_rule}"
    fi
    printf '    meta skuid "hubinetops" drop\n'
    printf '  }\n'
    printf '}\n'
  } >"${ruleset_tmp}"

  run_logged pct push "${VMID}" "${ruleset_tmp}" "${CT_NFT_CONF_PATH}" \
    || die "failed to push generated nftables ruleset into container ${VMID}"

  # Syntactically validated before activation, per the mandatory-gate
  # requirement -- `nft -c` checks syntax without loading the ruleset.
  run_logged pct exec "${VMID}" -- nft -c -f "${CT_NFT_CONF_PATH}" \
    || die "generated nftables ruleset failed syntax validation inside container ${VMID} -- refusing to activate it"

  run_logged pct exec "${VMID}" -- systemctl enable nftables \
    || die "failed to enable the nftables service inside container ${VMID}"
  run_logged pct exec "${VMID}" -- systemctl restart nftables \
    || die "failed to activate the nftables ruleset inside container ${VMID}"

  _verify_firewall_active

  log_pass "firewall: HA ${HA_SOURCE_CIDR} -> 8787 allowed, all other 8787 ingress denied, hubinetops egress confined to ${pve_host_rule_target}:8006$( [[ -n "${dns_rule}" ]] && printf ' + resolver %s:53' "${DNS_RESOLVER_IP}" )"
}

_endpoint_host() {
  local url="$1"
  # https://host[:port] -> host
  local rest="${url#https://}"
  rest="${rest%%/*}"
  rest="${rest%%:*}"
  printf '%s' "${rest}"
}

_verify_firewall_active() {
  local ruleset
  ruleset="$(pct exec "${VMID}" -- nft list ruleset 2>/dev/null)" \
    || die "could not read back the active nftables ruleset from container ${VMID} to verify it"

  [[ "${ruleset}" == *"ip saddr ${HA_SOURCE_CIDR} tcp dport 8787 accept"* ]] \
    || die "firewall verification failed: HA source ${HA_SOURCE_CIDR} -> 8787 allow rule not found in the active ruleset"
  [[ "${ruleset}" == *"tcp dport 8787 drop"* ]] \
    || die "firewall verification failed: default-deny for 8787 not found in the active ruleset"
  [[ "${ruleset}" == *'meta skuid "hubinetops" drop'* ]] \
    || die "firewall verification failed: hubinetops egress default-deny not found in the active ruleset"
  [[ "${ruleset}" == *"tcp dport 8006 accept"* ]] \
    || die "firewall verification failed: hubinetops -> PVE:8006 allow rule not found in the active ruleset"
}
