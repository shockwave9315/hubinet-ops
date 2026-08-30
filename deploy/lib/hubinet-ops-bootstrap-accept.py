#!/usr/bin/env python3
"""Hubinet Ops 0.5 R0 bootstrap discovery-acceptance check.

Invoked INSIDE the newly-created CT (via `pct exec <vmid> -- python3
<this-file> <expected-source-display-name> <timeout-seconds>`), never on
the Proxmox host. python3 is guaranteed present inside the CT because
deploy/install-0.5.0-fresh.sh already requires it there.

Secret handling: the R0 API bearer token is read directly from
/etc/hubinet-ops/agent.env (a 0640 root:hubinetops file already present on
this filesystem) INSIDE this script. It is never passed to this script as
a command-line argument or an environment variable, so it never appears in
this process's own argv/environ (both visible via /proc/<pid> while the
process runs) and the calling bootstrap script never logs it.

Field names used below are exactly the ones defined by
app/inventory/publication.py (published snapshot shape) and
custom_components/hubinet_ops/contract/enums.py (enum member values) in
this repository at the time this script was written -- nothing here is
invented.

Proves a genuinely COMMITTED, fresh, current discovery success -- not
merely `health == "healthy"` in isolation (a single enum snapshot cannot,
by itself, distinguish "committed, fresh, and matches the currently
configured source" from a stale/partial/misleading combination of
independently-updated fields). On PASS this script has confirmed, for the
single configured source:
  - health == "healthy" and freshness == "fresh"
  - latest_completed_outcome == "success" (the literal value
    app/inventory/authority.py's finalize_successful_discovery_run sets on
    a genuine committed success; never "invalid"/"partial"/
    "configuration_error"/"source_unavailable" from any degraded/rejected
    completion path)
  - last_committed_run_sequence is a real, positive sequence number
  - last_successful_observed_at is non-null
  - committed_context is non-null AND matches current_context field-for-
    field (source_config_revision, endpoint_id, canonical_transport_locator,
    canonicalization_contract_version, transport_trust_revision) --
    proving the committed state corresponds to the currently active
    configuration, not a stale prior commit
  - nodes is a non-empty list (a real PVE source has at least its own
    node; an empty nodes[] after a "healthy" commit is suspicious and
    withheld from PASS, even though resources[] may legitimately be empty)

For every resource the backend reports (zero is legitimate on a fresh
install -- resources[] is deliberately NOT required to be non-empty):
  - state_level == "discovered"
  - security_continuity != "trusted"
  - effective_capabilities is empty
  - presence != "confirmed_removed" (not a meaningful state on an initial
    R0 bootstrap pass, since nothing has ever been observed missing yet)

Changes no runtime state -- this script only ever reads the
already-published snapshot; see app/inventory/publication.py for the
read side.

Prints one final line to stdout: "PASS ..." or "FAIL <reason>", plus INFO
lines for diagnostics. Exit code 0 only on a genuine PASS.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

AGENT_ENV_PATH = "/etc/hubinet-ops/agent.env"
BASE_URL = "http://127.0.0.1:8787/r0/v1"
TOKEN_ENV_KEY = "HUBINET_OPS_R0_API_TOKEN"

# custom_components/hubinet_ops/contract/enums.py::SourceHealth
TERMINAL_FAILURE_HEALTH = {"source_unavailable", "configuration_error"}
HEALTHY = "healthy"
FRESH = "fresh"

# app/inventory/authority.py::finalize_successful_discovery_run -- the
# exact literal string set on latest_completed_outcome/last_run_health_
# outcome only by a genuine committed success. Every other value
# (BaselineCompleteness's "complete"/"partial"/"configuration_error"/
# "source_unavailable"/"invalid") represents a degraded or rejected
# completion, never a proof of committed success.
SUCCESSFUL_OUTCOME = "success"

# custom_components/hubinet_ops/contract/enums.py::ResourceStateLevel /
# SecurityContinuity / PresenceState
EXPECTED_STATE_LEVEL = "discovered"
FORBIDDEN_SECURITY_CONTINUITY = "trusted"
FORBIDDEN_PRESENCE = "confirmed_removed"

# app/inventory/publication.py::_source -- the exact field set that must
# match between current_context and committed_context to prove the
# committed state corresponds to the currently active configuration.
CONTEXT_FIELDS = (
    "source_config_revision",
    "endpoint_id",
    "canonical_transport_locator",
    "canonicalization_contract_version",
    "transport_trust_revision",
)

POLL_INTERVAL_SECONDS = 2


def read_bearer_token() -> str:
    with open(AGENT_ENV_PATH, "r", encoding="utf-8") as fh:
        for line in fh:
            if line.startswith(f"{TOKEN_ENV_KEY}="):
                return line.rstrip("\n").split("=", 1)[1]
    raise RuntimeError(f"{TOKEN_ENV_KEY} not found in {AGENT_ENV_PATH}")


def get_json(path: str, token: str) -> dict:
    req = urllib.request.Request(f"{BASE_URL}{path}")
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 -- fixed local URL
        return json.loads(resp.read().decode("utf-8"))


def _check_committed_source(source: dict, *, min_sequence_exclusive: int = 0) -> str | None:
    """Returns None if the source proves a genuinely committed, fresh,
    current success; otherwise a FAIL reason string (never raises -- the
    caller decides whether a given reason is worth continuing to poll
    for, e.g. simply not-yet-fresh, versus an immediate terminal stop).
    """
    if source.get("latest_completed_outcome") != SUCCESSFUL_OUTCOME:
        return f"latest-completed-outcome-not-success got={source.get('latest_completed_outcome')!r}"

    sequence = source.get("last_committed_run_sequence")
    if not isinstance(sequence, int) or sequence <= 0:
        return f"last-committed-run-sequence-invalid got={sequence!r}"
    if min_sequence_exclusive and sequence <= min_sequence_exclusive:
        return (
            "committed-sequence-not-past-baseline "
            f"got={sequence!r} baseline={min_sequence_exclusive!r}"
        )

    if not source.get("last_successful_observed_at"):
        return "last-successful-observed-at-missing"

    committed = source.get("committed_context")
    current = source.get("current_context")
    if not committed:
        return "committed-context-missing"
    if not current:
        return "current-context-missing"
    for field in CONTEXT_FIELDS:
        if committed.get(field) != current.get(field):
            return (
                f"committed-current-context-mismatch field={field} "
                f"committed={committed.get(field)!r} current={current.get(field)!r}"
            )

    return None


def main() -> int:
    # A 4th, optional argument lets the in-place updater
    # (deploy/lib/update-activate.sh) reuse this exact acceptance contract
    # for a POST-UPDATE, DB-PRESERVING check instead of copying it: when
    # given and > 0, the committed run sequence must exceed this floor,
    # proving a genuine completed discovery cycle happened AFTER the
    # update restarted the service, not merely that the pre-update state
    # was still being reported. Omitted (or "0"), this is the original,
    # unextended bootstrap contract -- any positive sequence passes,
    # exactly as before.
    if len(sys.argv) not in (3, 4):
        print(
            "FAIL usage: hubinet-ops-bootstrap-accept.py <expected-display-name> "
            "<timeout-seconds> [min-committed-sequence-exclusive]"
        )
        return 1
    expected_display_name = sys.argv[1]
    try:
        timeout_seconds = int(sys.argv[2])
    except ValueError:
        print("FAIL invalid timeout-seconds argument")
        return 1
    min_committed_sequence_exclusive = 0
    if len(sys.argv) == 4:
        try:
            min_committed_sequence_exclusive = int(sys.argv[3])
        except ValueError:
            print("FAIL invalid min-committed-sequence-exclusive argument")
            return 1
        if min_committed_sequence_exclusive < 0:
            print("FAIL invalid min-committed-sequence-exclusive argument")
            return 1

    try:
        token = read_bearer_token()
    except (OSError, RuntimeError) as exc:
        print(f"FAIL could-not-read-bearer-token {exc}")
        return 1

    try:
        backend = get_json("/backend", token)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"FAIL backend-endpoint-unreachable {exc}")
        return 1
    backend_instance_id = backend.get("backend_instance_id")
    if not isinstance(backend_instance_id, str) or not backend_instance_id:
        print("FAIL backend-instance-id-missing-or-invalid")
        return 1
    print(f"INFO backend_instance_id={backend_instance_id}")

    deadline = time.monotonic() + timeout_seconds
    last_health = "unknown"
    while True:
        try:
            snapshot = get_json("/snapshot", token)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print(f"FAIL snapshot-endpoint-unreachable {exc}")
            return 1

        sources = snapshot.get("sources")
        if not isinstance(sources, list) or len(sources) != 1:
            print(f"FAIL unexpected-source-count got={sources!r}")
            return 1
        source = sources[0]
        if source.get("name") != expected_display_name:
            print(f"FAIL source-name-mismatch expected={expected_display_name!r} got={source.get('name')!r}")
            return 1

        health = source.get("health")
        last_health = health
        if health in TERMINAL_FAILURE_HEALTH:
            print(f"FAIL source-health-terminal-failure health={health}")
            return 1

        if health == HEALTHY:
            if source.get("freshness") != FRESH:
                # Healthy-but-stale is a real, legitimate transient state
                # (e.g. right at a freshness-deadline boundary) -- keep
                # polling rather than treating it as terminal, but never
                # PASS on it.
                last_health = f"healthy/{source.get('freshness')}"
            else:
                committed_check_failure = _check_committed_source(
                    source, min_sequence_exclusive=min_committed_sequence_exclusive
                )
                if committed_check_failure is not None:
                    print(f"FAIL {committed_check_failure}")
                    return 1

                nodes = snapshot.get("nodes")
                if not isinstance(nodes, list) or len(nodes) == 0:
                    print(f"FAIL zero-nodes-after-healthy-commit got={nodes!r}")
                    return 1

                resources = snapshot.get("resources")
                if not isinstance(resources, list):
                    print("FAIL resources-field-missing-or-invalid")
                    return 1
                for resource in resources:
                    state_level = resource.get("state_level")
                    if state_level != EXPECTED_STATE_LEVEL:
                        print(f"FAIL resource-state-level-not-discovered got={state_level}")
                        return 1
                    security_continuity = resource.get("security_continuity")
                    if security_continuity == FORBIDDEN_SECURITY_CONTINUITY:
                        print(f"FAIL resource-security-continuity-trusted resource={resource.get('resource_id')}")
                        return 1
                    effective_capabilities = resource.get("effective_capabilities")
                    if effective_capabilities:
                        print(f"FAIL resource-has-effective-capabilities got={effective_capabilities!r}")
                        return 1
                    presence = resource.get("presence")
                    if presence == FORBIDDEN_PRESENCE:
                        print(f"FAIL resource-presence-confirmed-removed resource={resource.get('resource_id')}")
                        return 1

                print(
                    f"PASS backend_instance_id={backend_instance_id} "
                    f"source_health=healthy source_freshness=fresh "
                    f"last_committed_run_sequence={source.get('last_committed_run_sequence')} "
                    f"node_count={len(nodes)} resource_count={len(resources)}"
                )
                return 0

        if time.monotonic() >= deadline:
            print(f"FAIL discovery-timeout last_health={last_health}")
            return 1
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
