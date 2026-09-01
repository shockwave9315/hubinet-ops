"""Authority SQLite writer-contention policy.

PR #67 made `InventoryAuthority.execute_snapshot_submission_if_current` and
`InventoryAuthority.resolve_pre_submission_block` deliberately hold
`InventoryAuthorityStore`'s one `BEGIN IMMEDIATE` writer lock across one
bounded SSH host round trip each. That serialization closes a real
check-then-commit race and stays load-bearing; see
`app/inventory/authority.py` and `ARCHITECTURE.md`.

Holding the writer lock across a host round trip means any *other* writer
(discovery reconciliation, a package scan, plan approval) waiting on that same
lock must be willing to wait at least as long as the snapshot critical
section can legitimately run -- otherwise a perfectly healthy snapshot
operation makes an unrelated writer fail with `sqlite3.OperationalError:
database is locked`. Before PR #67's follow-up, every connection shared one
fixed `BUSY_TIMEOUT_MS = 5_000` with no relationship to that critical
section's real duration at all.

This module is the single source of truth for that relationship. It defines
the maximum a snapshot host critical section may legitimately run, and
derives the authority store's writer wait budget from it, so the two can
never silently diverge again. Nothing here changes what the writer lock is
held across (see the modules above) -- this is wait *policy*, not transaction
shape.
"""

from __future__ import annotations


#: Deliberate upper bound on `timeout_seconds` for
#: `SshPackageUpdateSnapshotHostControl` -- the wall-clock budget for ONE
#: bounded SSH round trip that runs a single typed operation
#: (`ensure_pre_update_snapshot_submitted`, `seal_operation_never_submitted`,
#: or the read-only `inspect_job_snapshot_state`) against the dark snapshot
#: helper. Both mutating operations are submission/seal-only and never poll a
#: PVE task to completion -- see `app/package_update_snapshot.py` and
#: `deploy/hubinet-package-snapshot-helper.py` -- so the call this bounds is
#: one `pvesh` trigger plus a durable journal write, not PVE's own
#: asynchronous snapshot task. The existing canonical timeout used by tests
#: (60s) is ordinary evidence of a healthy round trip, not this ceiling.
#: 90s gives a real, finite margin above that for a degraded network without
#: making the derived writer budget below open-ended: unlike the previous
#: unbounded 3600s ceiling, a caller can no longer legally configure a host
#: round trip long enough to make ordinary writer contention unbounded too.
MAX_SNAPSHOT_HOST_TIMEOUT_SECONDS = 90

#: The bounded process runner (`app.package_scan_host_control._bounded_process_runner`,
#: reused by the snapshot host-control transport) enforces `timeout_seconds`
#: itself by killing the subprocess, then reaps it with this fixed
#: `Popen.wait` allowance. That reap happens after `timeout_seconds` has
#: already elapsed but before the runner -- and therefore the writer
#: transaction awaiting its result -- returns control to the caller, so it is
#: part of the critical section's real wall-clock bound, not slack the
#: caller gets for free.
BOUNDED_PROCESS_CLEANUP_SECONDS = 5

#: The worst-case wall-clock duration ONE snapshot host critical section
#: (`execute_snapshot_submission_if_current` or `resolve_pre_submission_block`)
#: may legitimately hold the authority store's writer lock: the host round
#: trip itself, plus the bounded runner's own cleanup allowance if that round
#: trip actually times out.
MAX_SNAPSHOT_HOST_CRITICAL_SECTION_SECONDS = (
    MAX_SNAPSHOT_HOST_TIMEOUT_SECONDS + BOUNDED_PROCESS_CLEANUP_SECONDS
)

#: Explicit scheduling margin on top of the worst-case critical section --
#: OS/interpreter thread scheduling jitter, SQLite's own lock-handoff
#: bookkeeping, and clock coarseness -- so the writer budget below is not
#: sized to the exact theoretical worst case with zero room.
WRITER_SCHEDULING_MARGIN_SECONDS = 10

#: The wait budget every authority connection uses -- both `PRAGMA
#: busy_timeout` and the Python `sqlite3.connect(timeout=...)` -- when
#: attempting to become SQLite's one writer. This does not, and is not meant
#: to, guarantee fairness across arbitrarily many queued writers, freedom
#: from starvation under continuous write load, or recovery from a
#: permanently wedged transaction; it guarantees the ONE thing PR #67 left
#: open: one healthy bounded snapshot critical section cannot, by itself,
#: exhaust an ordinary concurrent writer's wait budget. A writer that is
#: still blocked after this budget still fails with `database is locked` --
#: this policy keeps that failure meaningful evidence of real, unbounded
#: contention instead of routine noise, it does not remove it.
AUTHORITY_WRITER_WAIT_BUDGET_SECONDS = (
    MAX_SNAPSHOT_HOST_CRITICAL_SECTION_SECONDS + WRITER_SCHEDULING_MARGIN_SECONDS
)

AUTHORITY_WRITER_WAIT_BUDGET_MS = int(AUTHORITY_WRITER_WAIT_BUDGET_SECONDS * 1_000)

# Machine-enforced relationship (not merely documented): it must be
# impossible for this module to describe a writer budget that a maximal
# legal snapshot host critical section could still exhaust. If either
# constant above is ever edited so this no longer holds, every import of
# this module fails immediately instead of silently reintroducing the
# original liveness bug.
assert AUTHORITY_WRITER_WAIT_BUDGET_SECONDS > MAX_SNAPSHOT_HOST_CRITICAL_SECTION_SECONDS, (
    "authority writer wait budget must exceed the maximum bounded snapshot "
    "host critical-section duration plus its scheduling margin"
)
