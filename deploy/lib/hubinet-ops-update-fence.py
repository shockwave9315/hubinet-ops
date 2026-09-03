#!/usr/bin/env python3
"""Acquire the exclusive product-update maintenance fence.

Invoked INSIDE the Hubinet CT (via `pct exec <vmid> -- python3 <this-file>
<holder>`), immediately before the in-place updater enters its mutation
window. Structurally parallel to deploy/lib/hubinet-ops-update-probe.py: one
bounded authenticated call against the already-running service, printing one
JSON object and always exiting 0 unless argv itself is unusable, so the bash
caller decides what a given answer means.

Why this exists rather than a second "is a job active?" read
------------------------------------------------------------

The probe's `package_update_active` answer is a poll, and a poll cannot make
two things exclusive. Between any such answer and the updater's first mutation
an authenticated operator may legitimately start a workload update, and a
second, later poll only moves that window instead of closing it -- the update
API stays live right up to the service stop.

So the backend does the work here, inside its own authority writer
transaction: it proves no workload package-update job is ACTIVE and makes the
fence durable in the same critical section that a workload `start_update`
would have to enter to create one. Exactly one of the two can win. From the
moment this returns `ok: true`, every new workload start refuses until the
fence is released.

Releasing is NOT done here. It is a plain filesystem removal the updater
performs at a terminal point -- a proven successful update, or a proven
complete rollback/recovery. It needs no atomicity (removing a fence only ever
widens what is permitted) and it must keep working when a failed activation
update has rolled back to a pre-activation backend that has no fence route at
all.

Secret handling: the R0 API bearer token is read directly from
/etc/hubinet-ops/agent.env inside this script, exactly like
hubinet-ops-update-probe.py -- it never appears in this process's own
argv/environ from the caller's side, and the calling shell script never logs
it.

  {"ok": true, "holder": "...", "acquired_at": "..."}
  {"ok": false, "reason": "<short-code>", "detail": "<bounded message>"}
"""
from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request

AGENT_ENV_PATH = "/etc/hubinet-ops/agent.env"
BASE_URL = "http://127.0.0.1:8787/r0/v1"
FENCE_PATH = "/package-update/maintenance-fence"
TOKEN_ENV_KEY = "HUBINET_OPS_R0_API_TOKEN"
TIMEOUT_SECONDS = 15

_HOLDER_RE = re.compile(r"^[0-9A-Za-z-]{1,64}$")


def read_bearer_token() -> str:
    with open(AGENT_ENV_PATH, "r", encoding="utf-8") as fh:
        for line in fh:
            if line.startswith(f"{TOKEN_ENV_KEY}="):
                return line.rstrip("\n").split("=", 1)[1]
    raise RuntimeError(f"{TOKEN_ENV_KEY} not found in {AGENT_ENV_PATH}")


def _emit(payload: dict) -> int:
    print(json.dumps(payload, separators=(",", ":")))
    return 0


def acquire(holder: str, token: str) -> dict:
    body = json.dumps({"holder": holder}).encode("utf-8")
    request = urllib.request.Request(f"{BASE_URL}{FENCE_PATH}", data=body, method="POST")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(  # noqa: S310 - fixed loopback URL
            request, timeout=TIMEOUT_SECONDS
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            error_body = json.loads(exc.read().decode("utf-8"))
            if isinstance(error_body, dict) and isinstance(
                error_body.get("detail"), dict
            ):
                detail = str(error_body["detail"].get("message", ""))[:500]
        except (UnicodeDecodeError, ValueError, OSError):
            detail = ""
        if exc.code == 404:
            # A backend predating production activation has no fence route,
            # and no update worker either -- it cannot own a workload job, so
            # there is nothing to make exclusive. Reported distinctly so the
            # caller can proceed rather than refuse.
            return {"ok": False, "reason": "fence_route_absent", "detail": detail}
        return {
            "ok": False,
            "reason": f"fence_refused_http_{exc.code}",
            "detail": detail,
        }
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {
            "ok": False,
            "reason": "fence_endpoint_unreachable",
            "detail": str(exc)[:500],
        }

    holder_out = payload.get("holder") if isinstance(payload, dict) else None
    acquired_at = payload.get("acquired_at") if isinstance(payload, dict) else None
    if holder_out != holder or not isinstance(acquired_at, str) or not acquired_at:
        return {
            "ok": False,
            "reason": "fence_response_malformed",
            "detail": "the backend did not confirm this exact fence holder",
        }
    return {"ok": True, "holder": holder_out, "acquired_at": acquired_at}


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not _HOLDER_RE.fullmatch(argv[1]):
        print("usage: hubinet-ops-update-fence.py <holder>", file=sys.stderr)
        return 2
    try:
        token = read_bearer_token()
    except (OSError, RuntimeError) as exc:
        return _emit(
            {
                "ok": False,
                "reason": "could_not_read_bearer_token",
                "detail": str(exc)[:500],
            }
        )
    return _emit(acquire(argv[1], token))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
