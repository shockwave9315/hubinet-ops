#!/usr/bin/env python3
"""Pre-update live-installation probe for the in-place Hubinet Ops updater.

Invoked INSIDE the Hubinet CT (via `pct exec <vmid> -- python3 <this-file>`),
never on the Proxmox host, and only ever BEFORE any managed-state mutation.
Structurally parallel to deploy/lib/hubinet-ops-bootstrap-accept.py, but
deliberately distinct from it: bootstrap-accept.py polls, with a bounded
timeout, until a *fresh* healthy commit is observed (the correct contract
right after a brand-new install); this probe takes exactly ONE bounded
read of whatever the already-running service currently reports, since an
established installation may legitimately be between discovery cycles,
mid-scan, or momentarily degraded, and the updater's job here is only to
prove "this is a live, reachable, currently-installed product" and to
capture its `backend_instance_id` / `last_committed_run_sequence` for the
later post-update comparison -- not to require freshness before touching
anything.

Secret handling: the R0 API bearer token is read directly from
/etc/hubinet-ops/agent.env inside this script, exactly like
hubinet-ops-bootstrap-accept.py -- it never appears in this process's own
argv/environ from the caller's side, and the calling shell script never
logs it.

Prints one JSON object to stdout and always exits 0 (the caller decides
what a given "ok": false reason means -- e.g. AGENTS.md's "missing/
zero-length authority DB on an allegedly installed/active product must
FAIL CLOSED" is enforced by the bash caller inspecting this JSON, not by
a nonzero exit here) unless argv itself is unusable:

  {"ok": true, "backend_instance_id": "...", "service_active": true,
   "last_committed_run_sequence": <int|null>, "health": "...",
   "freshness": "..."}
  {"ok": false, "reason": "<short-code>"}
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

AGENT_ENV_PATH = "/etc/hubinet-ops/agent.env"
BASE_URL = "http://127.0.0.1:8787/r0/v1"
TOKEN_ENV_KEY = "HUBINET_OPS_R0_API_TOKEN"
TIMEOUT_SECONDS = 5


def read_bearer_token() -> str:
    with open(AGENT_ENV_PATH, "r", encoding="utf-8") as fh:
        for line in fh:
            if line.startswith(f"{TOKEN_ENV_KEY}="):
                return line.rstrip("\n").split("=", 1)[1]
    raise RuntimeError(f"{TOKEN_ENV_KEY} not found in {AGENT_ENV_PATH}")


def get_json(path: str, token: str) -> dict:
    req = urllib.request.Request(f"{BASE_URL}{path}")
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    try:
        token = read_bearer_token()
    except (OSError, RuntimeError) as exc:
        print(json.dumps({"ok": False, "reason": f"could_not_read_bearer_token: {exc}"}))
        return 0

    try:
        backend = get_json("/backend", token)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "reason": f"backend_endpoint_unreachable: {exc}"}))
        return 0
    backend_instance_id = backend.get("backend_instance_id")
    if not isinstance(backend_instance_id, str) or not backend_instance_id:
        print(json.dumps({"ok": False, "reason": "backend_instance_id_missing_or_invalid"}))
        return 0

    try:
        snapshot = get_json("/snapshot", token)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "reason": f"snapshot_endpoint_unreachable: {exc}"}))
        return 0

    sources = snapshot.get("sources")
    if not isinstance(sources, list) or len(sources) != 1:
        print(json.dumps({"ok": False, "reason": "unexpected_source_count"}))
        return 0
    source = sources[0]

    sequence = source.get("last_committed_run_sequence")
    if sequence is not None and (not isinstance(sequence, int) or sequence <= 0):
        print(json.dumps({"ok": False, "reason": "last_committed_run_sequence_malformed"}))
        return 0

    print(json.dumps({
        "ok": True,
        "backend_instance_id": backend_instance_id,
        "service_active": True,
        "last_committed_run_sequence": sequence,
        "health": source.get("health"),
        "freshness": source.get("freshness"),
    }, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
