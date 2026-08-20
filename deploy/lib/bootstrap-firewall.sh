#!/usr/bin/env bash
# Phase 10 -- mandatory R0 firewall boundary, automated per
# deploy/README-0.5-firewall.md's nftables model: egress confined to the
# `hubinetops` service user (meta skuid), ingress restricted to the
# configured HA host/subnet on TCP 8787 only, loopback always reachable,
# and reply traffic on an already-accepted connection always permitted
# back out. Egress destination port is always derived from the actual
# configured PVE endpoint (--pve-endpoint), never hardcoded independently
# of it.
#
# Stateful model (fifth-pass corrective fix, P2-1 whole-feature review):
# an earlier version had no loopback exemption in `input` and no
# established/related exemption in `output`. Since 127.0.0.1 never
# matches HA_SOURCE_CIDR (an external host/subnet, by definition), this
# CT's own Phase 12 acceptance calls (http://127.0.0.1:8787) were dropped
# by input's unconditional "tcp dport 8787 drop" -- and even once that is
# fixed, the R0 backend's own HTTP replies (it runs AS the hubinetops
# user) to either a loopback client OR a real HA client would themselves
# be hubinetops-owned OUTPUT packets, silently dropped by the final "meta
# skuid hubinetops drop" with no established/related exemption ahead of
# it -- the inbound SYN would be accepted while every reply vanished. The
# corrected model, exactly the classic stateful-firewall shape:
#   input:  loopback always in; HA -> 8787 in; everything else on 8787
#           dropped.
#   output: replies to any already-accepted connection always out;
#           hubinetops NEW connections only to the resolved PVE IP(s) and
#           (when configured) the exact DNS resolver; everything else
#           hubinetops originates is dropped.
# `ct state established,related` matches only packets of a flow this
# firewall already let in -- it grants no new capability to originate
# connections; a hubinetops-initiated NEW connection to anywhere not on
# the explicit allow-list below is still dropped exactly as before.
#
# Hostname PVE endpoints: nftables resolves a bare hostname in an address
# expression to numeric address(es) at rule-LOAD time, and `nft list
# ruleset` afterward reports the resolved numeric address, never the
# original hostname text -- an exact-text verifier expecting the literal
# hostname back would then fail closed against an otherwise-correct
# configuration. The fix: `--pve-endpoint` hostnames are resolved to
# concrete IPv4 addresses ONCE, here, inside the CT's own network/resolver
# context (via deploy/lib/hubinet-ops-bootstrap-resolve-dns.py, python3,
# already guaranteed present by install-0.5.0-fresh.sh) -- BEFORE the
# ruleset is ever generated -- and one exact egress-allow rule is written
# per resolved address, so verification compares literal numeric IPs on
# both sides. RESOLVED_PVE_IPS (global, set once by phase10_firewall) is
# reused by the later phase12 recheck rather than re-resolved, since DNS
# is inherently time-varying, unlike the rest of this bootstrap's
# configuration -- see docs/README-bootstrap-proxmox-0.5.md: if internal
# DNS later moves the hostname to a new address, the firewall must be
# regenerated (re-run this bootstrap) before the service can reach it;
# this bootstrap never silently re-resolves and re-opens egress on its own
# after the fact.
#
# DNS resolver authority (P2-2, third pass): --dns-resolver is now
# authoritative for the fresh container, not merely a firewall-rule hint.
# phase1_preflight validates it and phase3 (bootstrap-container.sh) passes
# it to `pct create --nameserver`, so PVE itself regenerates the
# container's /etc/resolv.conf from it at every boot. Before any hostname
# resolution or rule generation here, _verify_ct_dns_resolver_matches_declared
# re-reads both the container's PVE config and its live /etc/resolv.conf
# and hard-stops on any mismatch -- the firewall's permitted DNS
# destination and the resolver the container will actually use are
# structurally proven to be the same address, not merely asserted to be.

CT_NFT_CONF_PATH="/etc/nftables.conf"
CT_RESOLVE_SCRIPT_CT="/tmp/hubinet-ops-bootstrap-resolve-dns.py"

# Set once by phase10_firewall, reused (never re-resolved) by the phase12
# recheck within the same bootstrap invocation. Newline-joined string
# (not a bash array) so it survives being read by helper functions without
# needing `declare -g` array plumbing.
RESOLVED_PVE_IPS_LIST=""

# _endpoint_host / _endpoint_port: pure functions of PVE_ENDPOINT
# (https://host[:port]). Deterministic string parsing of a fixed config
# value -- safe to call independently from both generation and
# verification. DNS resolution (below) is NOT re-derived the same way,
# because it is inherently time-varying.
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

# _resolve_pve_endpoint_ips <host>: prints one IPv4 address per line. If
# `host` is already a literal IPv4 address, prints it directly (no
# resolution needed, no DNS rule required later). Otherwise pushes and
# runs hubinet-ops-bootstrap-resolve-dns.py INSIDE the CT and requires at
# least one resolved address -- zero usable addresses, or a resolution
# failure, is always a hard stop (die), never an empty-but-accepted
# firewall rule set.
_resolve_pve_endpoint_ips() {
  local host="$1"
  if is_valid_ipv4 "${host}"; then
    printf '%s\n' "${host}"
    return 0
  fi

  run_logged pct push "${VMID}" "${BOOTSTRAP_SCRIPT_DIR}/lib/hubinet-ops-bootstrap-resolve-dns.py" "${CT_RESOLVE_SCRIPT_CT}" \
    || die "failed to push DNS resolution script into container ${VMID}"

  local output status
  output="$(pct exec "${VMID}" -- python3 "${CT_RESOLVE_SCRIPT_CT}" "${host}" 2>&1)" && status=0 || status=$?
  # `tr -d '\r'` defensively strips any stray carriage return (see
  # bootstrap-common.sh's _json_truthy_keys_sorted for the same class of
  # fix and why) -- harmless on a normal POSIX PVE host/CT, but
  # load-bearing for the exact-address parsing/comparison below wherever
  # a CT's own python3 might emit CRLF line endings.
  output="$(printf '%s' "${output}" | tr -d '\r')"

  pct exec "${VMID}" -- rm -f "${CT_RESOLVE_SCRIPT_CT}" >/dev/null 2>&1 \
    || log_warn "could not remove ${CT_RESOLVE_SCRIPT_CT} inside the container (non-fatal)"

  (( status == 0 )) \
    || die "could not resolve PVE endpoint hostname '${host}' to any usable IPv4 address inside container ${VMID} (resolution is performed inside the CT's own network/resolver context): ${output:-no output}"

  printf '%s\n' "${output}"
}

# _expected_dns_rule_lines: prints the exact, non-indented expected DNS
# egress rule line(s) if the PVE endpoint is a hostname (requiring a
# scoped resolver rule), or nothing if it's a literal IP. Both UDP and TCP
# port 53 to the exact configured resolver are required (legitimate DNS
# responses may need TCP fallback, e.g. for larger responses) -- never a
# broader/public resolver. Dies if a hostname endpoint has no
# --dns-resolver configured.
_expected_dns_rule_lines() {
  local pve_host="$1"
  if is_valid_ipv4 "${pve_host}"; then
    return 0
  fi
  [[ -n "${DNS_RESOLVER_IP}" ]] \
    || die "source.pve_endpoint ('${PVE_ENDPOINT}') uses a hostname, not a literal IP -- pass --dns-resolver <your-internal-resolver-ip> so egress can be scoped narrowly, or reconfigure --pve-endpoint with a literal IP and omit --dns-resolver entirely"
  is_valid_ipv4 "${DNS_RESOLVER_IP}" || die "--dns-resolver '${DNS_RESOLVER_IP}' is not a valid IPv4 address"
  printf 'meta skuid "hubinetops" ip daddr %s udp dport 53 accept\n' "${DNS_RESOLVER_IP}"
  printf 'meta skuid "hubinetops" ip daddr %s tcp dport 53 accept\n' "${DNS_RESOLVER_IP}"
}

# _verify_ct_dns_resolver_matches_declared: third-pass corrective fix
# (P2-2). Structurally ties the firewall's permitted DNS destination to
# the resolver the container will ACTUALLY use, rather than trusting
# --dns-resolver as a mere firewall-rule hint while the container's own
# live resolver configuration is left to whatever it happened to be
# assigned. A mismatch here previously could not be detected until
# discovery started failing with an opaque DNS/network symptom well after
# the firewall had already activated. Only meaningful in hostname PVE
# endpoint mode (DNS_RESOLVER_IP is set only then, per phase1_preflight);
# a no-op in literal-IP mode. Verifies BOTH:
#   - the container's own PVE configuration (`pct config <vmid>`) records
#     the declared nameserver -- proves phase3's --nameserver was actually
#     accepted and persisted by PVE, not merely requested;
#   - the container's own LIVE /etc/resolv.conf (what glibc's resolver
#     actually reads at getaddrinfo() time) names ONLY the declared
#     resolver.
# A command failure, a missing/mismatched PVE config entry, an unreadable
# /etc/resolv.conf, zero nameserver entries, or any entry that is not
# exactly the declared resolver are all a hard stop before the firewall is
# ever generated -- never a warning that the operator "should" make them
# match. See deploy/README-bootstrap-proxmox-0.5.md's REAL-HOST PRECHECK
# section for the one real-environment assumption this check cannot
# itself verify offline: whether the selected Debian 13 standard template
# manages /etc/resolv.conf directly (PVE-injected content, verifiable
# exactly as below) or via a stub resolver layer (e.g. systemd-resolved,
# which would show 127.0.0.53 regardless of the real upstream) -- if the
# template uses a stub, this check will correctly, safely refuse to
# proceed rather than falsely claim a match it cannot prove.
_verify_ct_dns_resolver_matches_declared() {
  [[ -n "${DNS_RESOLVER_IP}" ]] || return 0

  local pct_conf
  pct_conf="$(pct config "${VMID}" 2>/dev/null)" \
    || die "could not read back container ${VMID}'s configuration to verify its nameserver setting"
  # `${DNS_RESOLVER_IP//./\\.}` (the dot literally inline in the pattern
  # position) does NOT actually escape anything in bash -- the backslash
  # is silently dropped, so a raw IP substitution would let stray '.'s
  # match as regex "any character" wildcards in the grep -E pattern
  # below. Routing the replacement text through an intermediate variable
  # first is the only form that reliably preserves the literal backslash.
  local dot_escape='\.'
  local escaped_resolver_ip="${DNS_RESOLVER_IP//./${dot_escape}}"
  printf '%s\n' "${pct_conf}" | grep -Eq "^nameserver:[[:space:]]*${escaped_resolver_ip}([[:space:]]|\$)" \
    || die "container ${VMID}'s PVE configuration does not record 'nameserver: ${DNS_RESOLVER_IP}' (declared via --dns-resolver) -- refusing to activate a firewall that will only ever permit DNS egress to that address while the container's authoritative resolver setting is unconfirmed. Check 'pct config ${VMID}' and re-run."

  local resolv_conf
  resolv_conf="$(pct exec "${VMID}" -- cat /etc/resolv.conf 2>/dev/null)" \
    || die "could not read /etc/resolv.conf inside container ${VMID} to verify its actual live resolver configuration"

  local -a live_resolvers=()
  local line
  while IFS= read -r line; do
    [[ "${line}" =~ ^nameserver[[:space:]]+([0-9.]+)[[:space:]]*$ ]] && live_resolvers+=("${BASH_REMATCH[1]}")
  done <<<"${resolv_conf}"

  [[ ${#live_resolvers[@]} -gt 0 ]] \
    || die "container ${VMID}'s /etc/resolv.conf declares no usable nameserver entries -- cannot verify it will actually use the declared --dns-resolver ${DNS_RESOLVER_IP}. If this template manages DNS via a stub resolver (e.g. systemd-resolved), hostname PVE endpoint mode is not currently supported by this bootstrap -- reconfigure --pve-endpoint with a literal IP instead."

  local ip mismatch=0
  for ip in "${live_resolvers[@]}"; do
    [[ "${ip}" == "${DNS_RESOLVER_IP}" ]] || mismatch=1
  done
  (( mismatch == 0 )) \
    || die "container ${VMID}'s live /etc/resolv.conf resolver(s) (${live_resolvers[*]}) do not exactly match the declared --dns-resolver (${DNS_RESOLVER_IP}) -- the firewall would permit DNS egress only to the declared resolver while the container actually queries a different one, silently breaking hostname resolution once the firewall activates. Refusing to proceed; re-run with the correct --dns-resolver value, or reconfigure --pve-endpoint with a literal IP to avoid DNS resolution entirely."

  log_pass "container ${VMID}'s DNS resolver configuration (PVE config + live /etc/resolv.conf) matches declared --dns-resolver ${DNS_RESOLVER_IP}"
}

phase10_firewall() {
  log_phase "Phase 10: firewall"

  local pve_host pve_port
  pve_host="$(_endpoint_host "${PVE_ENDPOINT}")"
  pve_port="$(_endpoint_port "${PVE_ENDPOINT}")"

  # Must hold BEFORE any resolution is attempted or any firewall rule is
  # generated -- see _verify_ct_dns_resolver_matches_declared above.
  _verify_ct_dns_resolver_matches_declared

  # Validate the DNS-resolver *configuration* before attempting any actual
  # resolution: a hostname endpoint with no --dns-resolver is a
  # configuration problem the operator needs to fix regardless of whether
  # resolution would otherwise have succeeded (the resolver IP is also
  # needed for the DNS allow-rule itself, not only as a resolution
  # prerequisite) -- fail on that first, with the specific actionable
  # message, rather than a generic resolution failure.
  local dns_rule_lines
  dns_rule_lines="$(_expected_dns_rule_lines "${pve_host}")"
  if [[ -n "${dns_rule_lines}" ]]; then
    log_info "PVE endpoint uses a hostname -- adding DNS egress rules (UDP+TCP 53) scoped to resolver ${DNS_RESOLVER_IP} only"
  fi

  RESOLVED_PVE_IPS_LIST="$(_resolve_pve_endpoint_ips "${pve_host}")"
  local -a resolved_ips=()
  while IFS= read -r ip; do
    [[ -n "${ip}" ]] && resolved_ips+=("${ip}")
  done <<<"${RESOLVED_PVE_IPS_LIST}"
  [[ ${#resolved_ips[@]} -gt 0 ]] \
    || die "PVE endpoint host '${pve_host}' resolved to zero usable IPv4 addresses -- refusing to generate a firewall rule set with no destination"
  if ! is_valid_ipv4 "${pve_host}"; then
    log_info "resolved PVE endpoint host '${pve_host}' to: ${resolved_ips[*]} (this exact set is what the firewall permits for the lifetime of this CT -- re-run this bootstrap if internal DNS later moves the hostname)"
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
    # Fifth-pass corrective fix (P2-1, whole-feature review): loopback
    # MUST be accepted before the HA-scoped 8787 rules, or Phase 12's own
    # required http://127.0.0.1:8787 acceptance calls (and any future
    # local health check) are dropped by the unconditional "tcp dport
    # 8787 drop" fallthrough below -- 127.0.0.1 never matches
    # HA_SOURCE_CIDR, which is by definition an external host/subnet.
    # Interface-based (iifname "lo"), not address-based (ip saddr
    # 127.0.0.1): loopback traffic can only ever originate from within
    # this CT's own network namespace, so this is not a spoofable trust
    # boundary widening -- it exactly restores the "local processes on
    # this host may always reach services on this host" property every
    # real firewall reference (including nftables' own documented base
    # ruleset) grants explicitly and unconditionally.
    printf '    iifname "lo" accept\n'
    printf '    ip saddr %s tcp dport 8787 accept\n' "${HA_SOURCE_CIDR}"
    printf '    tcp dport 8787 drop\n'
    printf '  }\n'
    printf '  chain output {\n'
    printf '    type filter hook output priority 0; policy accept;\n'
    # Fifth-pass corrective fix, part 2: the R0 backend runs AS the
    # hubinetops user, so its own HTTP replies to an already-accepted
    # inbound connection (a real HA client, or a local loopback
    # acceptance/health client) are themselves hubinetops-owned OUTPUT
    # packets -- without this rule they would fall through to the final
    # "meta skuid hubinetops drop" below and the connection would appear
    # to hang (SYN accepted, but every reply silently dropped). `ct state
    # established,related` matches only packets belonging to a connection
    # this firewall already let in (or a directly related flow) -- it
    # does NOT match the initial SYN of a NEW connection hubinetops
    # itself originates, so it grants no new *outbound-initiated*
    # capability at all; hubinetops-initiated NEW connections are still
    # evaluated only against the PVE/DNS allow-list immediately below,
    # then the final drop, exactly as before. Placed first so it also
    # covers the loopback health-check server's own replies, which are
    # otherwise indistinguishable (by ct state) from any other reply
    # traffic.
    printf '    ct state established,related accept\n'
    local ip
    for ip in "${resolved_ips[@]}"; do
      printf '    meta skuid "hubinetops" ip daddr %s tcp dport %s accept\n' "${ip}" "${pve_port}"
    done
    if [[ -n "${dns_rule_lines}" ]]; then
      # Here-string (<<<), not a pipe: a pipe fed via `printf '%s'` (no
      # trailing newline) makes `read`'s last iteration fail and silently
      # drop the final line (a real bug this exact form previously hit --
      # the TCP DNS rule went missing from the generated ruleset). A
      # here-string always appends its own trailing newline.
      while IFS= read -r line; do
        [[ -n "${line}" ]] && printf '    %s\n' "${line}"
      done <<<"${dns_rule_lines}"
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

  log_pass "firewall: loopback always reachable, HA ${HA_SOURCE_CIDR} -> 8787 allowed, all other 8787 ingress denied, established/related replies always permitted out, hubinetops NEW egress confined to {${resolved_ips[*]}}:${pve_port}$( [[ -n "${dns_rule_lines}" ]] && printf ' + resolver %s:53 (udp+tcp)' "${DNS_RESOLVER_IP}" )"
}

# _verify_firewall_active: exact rule content AND order within each
# chain -- not a loose "does this substring appear anywhere" check.
# Extracts the non-boilerplate rule lines of each chain (stripped of
# leading whitespace, skipping the `type filter hook ...` declaration
# line) and compares them element-by-element against the exact expected
# sequence. Egress IPs come from RESOLVED_PVE_IPS_LIST (set once by
# phase10_firewall, never re-resolved here) -- everything else
# (HA_SOURCE_CIDR, pve_port, DNS resolver) is a static config value, safe
# to re-derive independently of what generation assumed.
_verify_firewall_active() {
  local ruleset
  ruleset="$(pct exec "${VMID}" -- nft list ruleset 2>/dev/null)" \
    || die "acceptance failed: could not read the active nftables ruleset from container ${VMID}"

  local pve_host pve_port
  pve_host="$(_endpoint_host "${PVE_ENDPOINT}")"
  pve_port="$(_endpoint_port "${PVE_ENDPOINT}")"

  [[ -n "${RESOLVED_PVE_IPS_LIST}" ]] \
    || die "internal error: RESOLVED_PVE_IPS_LIST was not set by phase10_firewall before firewall verification"
  local -a resolved_ips=()
  while IFS= read -r ip; do
    [[ -n "${ip}" ]] && resolved_ips+=("${ip}")
  done <<<"${RESOLVED_PVE_IPS_LIST}"

  local dns_rule_lines
  dns_rule_lines="$(_expected_dns_rule_lines "${pve_host}")"

  local -a expected_input=(
    'iifname "lo" accept'
    "ip saddr ${HA_SOURCE_CIDR} tcp dport 8787 accept"
    "tcp dport 8787 drop"
  )
  local -a expected_output=(
    "ct state established,related accept"
  )
  local ip
  for ip in "${resolved_ips[@]}"; do
    expected_output+=("meta skuid \"hubinetops\" ip daddr ${ip} tcp dport ${pve_port} accept")
  done
  if [[ -n "${dns_rule_lines}" ]]; then
    local dns_line
    while IFS= read -r dns_line; do
      [[ -n "${dns_line}" ]] && expected_output+=("${dns_line}")
    done <<<"${dns_rule_lines}"
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
