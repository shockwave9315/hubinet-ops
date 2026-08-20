#!/usr/bin/env bash
# Phase 10 -- mandatory R0 firewall boundary, automated per
# deploy/README-0.5-firewall.md's nftables model: egress confined to the
# `hubinetops` service user (meta skuid), ingress restricted to the
# configured HA host/subnet on TCP 8787 only. Egress destination port is
# always derived from the actual configured PVE endpoint (--pve-endpoint),
# never hardcoded independently of it.

CT_NFT_CONF_PATH="/etc/nftables.conf"

# _endpoint_host / _endpoint_port: pure functions of PVE_ENDPOINT
# (https://host[:port]). Both the ruleset generator and the verifier call
# these independently rather than sharing mutable state, so verification
# never merely re-checks what generation assumed -- it re-derives the
# expected values from the same single source of truth (PVE_ENDPOINT).
_endpoint_host() {
  local url="$1"
  local rest="${url#https://}"
  rest="${rest%%/*}"
  rest="${rest%%:*}"
  printf '%s' "${rest}"
}

_endpoint_port() {
  local url="$1"
  local rest="${url#https://}"
  rest="${rest%%/*}"
  if [[ "${rest}" == *:* ]]; then
    printf '%s' "${rest##*:}"
  else
    # PVE's own documented default API port when none is given explicitly.
    printf '8006'
  fi
}

# _expected_dns_rule_line: prints the exact, non-indented expected DNS
# egress rule line if the PVE endpoint is a hostname (requiring a scoped
# resolver rule), or nothing if it's a literal IP. Dies if a hostname
# endpoint has no --dns-resolver configured.
_expected_dns_rule_line() {
  local pve_host="$1"
  if is_valid_ipv4 "${pve_host}"; then
    return 0
  fi
  [[ -n "${DNS_RESOLVER_IP}" ]] \
    || die "source.pve_endpoint ('${PVE_ENDPOINT}') uses a hostname, not a literal IP -- pass --dns-resolver <your-internal-resolver-ip> so egress can be scoped narrowly, or reconfigure --pve-endpoint with a literal IP and omit --dns-resolver entirely"
  is_valid_ipv4 "${DNS_RESOLVER_IP}" || die "--dns-resolver '${DNS_RESOLVER_IP}' is not a valid IPv4 address"
  printf 'meta skuid "hubinetops" ip daddr %s udp dport 53 accept' "${DNS_RESOLVER_IP}"
}

phase10_firewall() {
  log_phase "Phase 10: firewall"

  local pve_host pve_port dns_rule_line
  pve_host="$(_endpoint_host "${PVE_ENDPOINT}")"
  pve_port="$(_endpoint_port "${PVE_ENDPOINT}")"
  dns_rule_line="$(_expected_dns_rule_line "${pve_host}")"
  if [[ -n "${dns_rule_line}" ]]; then
    log_info "PVE endpoint uses a hostname -- adding a DNS egress rule scoped to resolver ${DNS_RESOLVER_IP} only"
  fi

  local ruleset_tmp
  ruleset_tmp="$(secret_tmpfile "/tmp/hubinet-ops-bootstrap-nft.XXXXXX.conf")"
  # Not secret, but the same restrictive-tempfile helper is reused for
  # convenience/guaranteed cleanup.
  {
    # No nft shebang directive here: this file is never chmod +x'd or
    # executed directly -- it is always loaded either via an explicit
    # `nft -c -f`/`nft -f` invocation from this script, or by the
    # nftables.service unit's own fixed ExecStart, so such a directive
    # would be decorative only.
    printf 'table inet hubinet_ops_r0 {\n'
    printf '  chain input {\n'
    printf '    type filter hook input priority 0; policy accept;\n'
    printf '    ip saddr %s tcp dport 8787 accept\n' "${HA_SOURCE_CIDR}"
    printf '    tcp dport 8787 drop\n'
    printf '  }\n'
    printf '  chain output {\n'
    printf '    type filter hook output priority 0; policy accept;\n'
    printf '    meta skuid "hubinetops" ip daddr %s tcp dport %s accept\n' "${pve_host}" "${pve_port}"
    if [[ -n "${dns_rule_line}" ]]; then
      printf '    %s\n' "${dns_rule_line}"
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

  log_pass "firewall: HA ${HA_SOURCE_CIDR} -> 8787 allowed, all other 8787 ingress denied, hubinetops egress confined to ${pve_host}:${pve_port}$( [[ -n "${dns_rule_line}" ]] && printf ' + resolver %s:53' "${DNS_RESOLVER_IP}" )"
}

# _verify_firewall_active: exact rule content AND order within each
# chain -- not a loose "does this substring appear anywhere" check.
# Extracts the non-boilerplate rule lines of each chain (stripped of
# leading whitespace, skipping the `type filter hook ...` declaration
# line) and compares them element-by-element against the exact expected
# sequence.
_verify_firewall_active() {
  local ruleset
  ruleset="$(pct exec "${VMID}" -- nft list ruleset 2>/dev/null)" \
    || die "acceptance failed: could not read the active nftables ruleset from container ${VMID}"

  local pve_host pve_port dns_rule_line
  pve_host="$(_endpoint_host "${PVE_ENDPOINT}")"
  pve_port="$(_endpoint_port "${PVE_ENDPOINT}")"
  dns_rule_line="$(_expected_dns_rule_line "${pve_host}")"

  local -a expected_input=(
    "ip saddr ${HA_SOURCE_CIDR} tcp dport 8787 accept"
    "tcp dport 8787 drop"
  )
  local -a expected_output=(
    "meta skuid \"hubinetops\" ip daddr ${pve_host} tcp dport ${pve_port} accept"
  )
  if [[ -n "${dns_rule_line}" ]]; then
    expected_output+=("${dns_rule_line}")
  fi
  expected_output+=('meta skuid "hubinetops" drop')

  _verify_chain_rules_exact "${ruleset}" "input" expected_input \
    || die "firewall verification failed: 'input' chain rules do not exactly match the expected content/order"
  _verify_chain_rules_exact "${ruleset}" "output" expected_output \
    || die "firewall verification failed: 'output' chain rules do not exactly match the expected content/order"
}

# _verify_chain_rules_exact <ruleset text> <chain name> <expected-array-name>
_verify_chain_rules_exact() {
  local ruleset="$1" chain_name="$2"
  local -n expected_ref="$3"

  local actual
  actual="$(printf '%s\n' "${ruleset}" | awk -v chain="chain ${chain_name} {" '
    $0 ~ chain { in_chain=1; next }
    in_chain && /^[[:space:]]*}/ { in_chain=0; next }
    in_chain {
      line=$0
      sub(/^[[:space:]]+/, "", line)
      if (line ~ /^type filter hook/) next
      if (line == "") next
      print line
    }
  ')"

  local -a actual_lines=()
  while IFS= read -r line; do
    [[ -n "${line}" ]] && actual_lines+=("${line}")
  done <<<"${actual}"

  if [[ "${#actual_lines[@]}" -ne "${#expected_ref[@]}" ]]; then
    log_warn "chain '${chain_name}': expected ${#expected_ref[@]} rule(s), found ${#actual_lines[@]} (${actual_lines[*]:-<none>})"
    return 1
  fi

  local i
  for (( i = 0; i < ${#expected_ref[@]}; i++ )); do
    if [[ "${actual_lines[$i]}" != "${expected_ref[$i]}" ]]; then
      log_warn "chain '${chain_name}' rule $(( i + 1 )) mismatch: expected '${expected_ref[$i]}', found '${actual_lines[$i]}'"
      return 1
    fi
  done
  return 0
}
