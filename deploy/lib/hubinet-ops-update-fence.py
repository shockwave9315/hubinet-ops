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

Pre-activation installations
----------------------------

A backend that predates production activation answers 404 here: it has no
fence route, and no workload update route either, so no race handshake with
it is possible or needed. But "no race with the OLD backend" is not "no fence
required for this run". The very next thing the updater does is activate the
new configuration and helpers and start the TARGET backend, whose
`/package-update` route IS live while Phase U5 acceptance is still running --
and an acceptance failure there rolls product backend and helper material back
underneath any workload job issued into that window.

So `--pre-activation` writes exactly the same durable fence artifact directly,
with the same holder semantics and the same fail-closed durability, before the
updater enters its mutation window. The target backend therefore finds the
fence already present the moment it starts, and refuses workload starts
throughout acceptance. It still never steals a fence another product update
holds.

Secret handling: the R0 API bearer token is read directly from
/etc/hubinet-ops/agent.env inside this script, exactly like
hubinet-ops-update-probe.py -- it never appears in this process's own
argv/environ from the caller's side, and the calling shell script never logs
it.

  {"ok": true, "holder": "...", "acquired_at": "..."}
  {"ok": false, "reason": "<short-code>", "detail": "<bounded message>"}
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import re
import sys
import urllib.error
import urllib.request

AGENT_ENV_PATH = "/etc/hubinet-ops/agent.env"
#: The one durable fence artifact. Identical to the path the backend derives
#: from its own authority database directory -- there is exactly one fence,
#: whichever side created it.
FENCE_FILE = "/var/lib/hubinet-ops/product-update-maintenance.fence"
BASE_URL = "http://127.0.0.1:8787/r0/v1"
FENCE_PATH = "/package-update/maintenance-fence"
TOKEN_ENV_KEY = "HUBINET_OPS_R0_API_TOKEN"

# --- Pre-ACK timeout contract -----------------------------------------
#
# `acquire()`'s POST is synchronous, and the backend route it calls
# (`acquire_product_update_maintenance_fence`) can, before it ever answers,
# wait to become the authority store's one SQLite writer -- the same
# `BEGIN IMMEDIATE` lock `issue_package_update_job` takes -- because that is
# the whole synchronization the fence relies on (see
# `app/inventory/product_update_fence.py`). A workload host-control critical
# section already legitimately holding that lock is not a bug; refusing to
# wait for it is. A client deadline shorter than the backend's own
# legitimate pre-ACK budget does not fail safe here -- it fails INTO the P1
# this timeout exists to close: the client abandons, this run's recovery
# journal is resolved/cleared on the belief the fence was never acquired,
# and the backend later durably creates the fence anyway, for a run that no
# longer exists to release it. See this module's own docstring.
#
# `AUTHORITY_WRITER_WAIT_BUDGET_SECONDS_MIRROR` is that budget, mirrored
# from `app.inventory.contention_policy.AUTHORITY_WRITER_WAIT_BUDGET_SECONDS`
# rather than imported: this script runs standalone inside the CT via
# `pct exec`, with no application import path (see the module docstring's
# "Pre-activation installations" section -- it must keep working against a
# backend that may not even be the one that shipped it). A regression test
# (tests/test_update_authority_helpers.py) asserts this mirror still equals
# the real backend constant, so drift fails a test instead of silently
# reintroducing the P1.
AUTHORITY_WRITER_WAIT_BUDGET_SECONDS_MIRROR = 105

#: Bounded margin for the route's own pre-ACK work once it actually holds
#: the writer lock: one `SELECT` against `package_update_jobs`, one small
#: fence-file read, and one fsynced fence-file write -- never another host
#: round trip and never the writer wait itself.
FENCE_ROUTE_PROCESSING_MARGIN_SECONDS = 5

#: Same bounded allowance every synchronous request in this control surface
#: gives on top of the backend's own processing ceiling for ordinary
#: HTTP/TLS/loopback overhead (matches
#: `custom_components/hubinet_ops/transport_http.py`'s
#: `_ROLLBACK_REQUEST_TIMEOUT` derivation).
NETWORK_MARGIN_SECONDS = 15

#: The client timeout itself. Must exceed the backend's maximum legitimate
#: pre-ACK budget (writer wait + its own bounded processing), never merely
#: approximate it -- see the contract note above.
TIMEOUT_SECONDS = (
    AUTHORITY_WRITER_WAIT_BUDGET_SECONDS_MIRROR
    + FENCE_ROUTE_PROCESSING_MARGIN_SECONDS
    + NETWORK_MARGIN_SECONDS
)

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


def _read_existing_fence() -> dict | None:
    """Read the durable fence, or None. Malformed content is NOT absence."""

    try:
        with open(FENCE_FILE, "rb") as handle:
            raw = handle.read(4097)
    except FileNotFoundError:
        return None
    if len(raw) > 4096:
        raise ValueError("fence exceeds its structural bound")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("holder"), str):
        raise ValueError("fence does not name a holder")
    return payload


def acquire_pre_activation(holder: str) -> dict:
    """Create the fence directly, for a backend that has no fence route.

    Same artifact, same holder semantics, same fail-closed durability as the
    backend-created fence: fsynced, renamed atomically into place, and the
    directory fsynced, so it is on disk before this returns success and is
    therefore already present when the target backend starts.

    No workload-job handshake is attempted or needed -- a pre-activation
    backend has no issuance route and no worker, so nothing can be racing
    this. A fence another product update already holds is still never
    stolen, and re-running for the same holder is idempotent.
    """

    try:
        existing = _read_existing_fence()
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return {
            "ok": False,
            "reason": "fence_unreadable",
            "detail": str(exc)[:500],
        }
    if existing is not None:
        if existing["holder"] != holder:
            return {
                "ok": False,
                "reason": "fence_held_by_another_run",
                "detail": (
                    "another Hubinet product update already holds the "
                    f"maintenance fence (holder {existing['holder']})"
                ),
            }
        return {
            "ok": True,
            "holder": holder,
            "acquired_at": str(existing.get("acquired_at", "")),
        }

    acquired_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload = json.dumps(
        {"holder": holder, "acquired_at": acquired_at},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    temporary = f"{FENCE_FILE}.tmp-{os.getpid()}"
    try:
        with open(temporary, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, FENCE_FILE)
        directory = os.open(os.path.dirname(FENCE_FILE), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        return {
            "ok": False,
            "reason": "fence_not_writable",
            "detail": str(exc)[:500],
        }
    return {"ok": True, "holder": holder, "acquired_at": acquired_at}


def main(argv: list[str]) -> int:
    arguments = argv[1:]
    pre_activation = False
    if arguments and arguments[-1] == "--pre-activation":
        pre_activation = True
        arguments = arguments[:-1]
    if len(arguments) != 1 or not _HOLDER_RE.fullmatch(arguments[0]):
        print(
            "usage: hubinet-ops-update-fence.py <holder> [--pre-activation]",
            file=sys.stderr,
        )
        return 2
    holder = arguments[0]

    if pre_activation:
        return _emit(acquire_pre_activation(holder))

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
    return _emit(acquire(holder, token))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
