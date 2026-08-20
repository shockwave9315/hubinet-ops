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
invented. A source stuck in a non-"healthy" SourceHealth state (in
particular the two terminal-failure states, SOURCE_UNAVAILABLE and
CONFIGURATION_ERROR) never yields PASS; DEGRADED and NOT_YET_OBSERVED are
treated as legitimate transient states worth continuing to poll for, up to
the given timeout.

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

# custom_components/hubinet_ops/contract/enums.py::ResourceStateLevel /
# SecurityContinuity
EXPECTED_STATE_LEVEL = "discovered"
FORBIDDEN_SECURITY_CONTINUITY = "trusted"

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


def main() -> int:
    if len(sys.argv) != 3:
        print("FAIL usage: hubinet-ops-bootstrap-accept.py <expected-display-name> <timeout-seconds>")
        return 1
    expected_display_name = sys.argv[1]
    try:
        timeout_seconds = int(sys.argv[2])
    except ValueError:
        print("FAIL invalid timeout-seconds argument")
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
            print(
                f"PASS backend_instance_id={backend_instance_id} "
                f"source_health=healthy resource_count={len(resources)}"
            )
            return 0

        if time.monotonic() >= deadline:
            print(f"FAIL discovery-timeout last_health={last_health}")
            return 1
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
