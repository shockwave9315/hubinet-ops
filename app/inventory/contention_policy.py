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
the maximum a host critical section may legitimately run, and derives the
authority store's writer wait budget from it, so the two can never silently
diverge again. Nothing here changes what the writer lock is held across (see
the modules above) -- this is wait *policy*, not transaction shape.

Crash-safe package mutation adds a second pair of such critical sections
(`execute_package_mutation_submission_if_current` and
`resolve_pre_mutation_block`), and same-job rollback execution adds a third
(`execute_rollback_submission_if_current` and `resolve_pre_rollback_block`),
for exactly the same reason and with exactly the same shape: one bounded host
round trip that crosses (or durably forbids) a submission boundary, never the
destructive operation itself. The budget below is therefore derived from the
worst case over *every* such critical section, so adding one can never
silently leave ordinary writers with a wait budget shorter than a healthy one
of them.
"""

from __future__ import annotations


#: Deliberate upper bound on `timeout_seconds` for
#: `SshPackageUpdateSnapshotHostControl` -- the wall-clock budget for ONE
#: bounded SSH round trip that runs a single typed operation
#: (`ensure_pre_update_snapshot_submitted`, `seal_operation_never_submitted`,
#: or the read-only `inspect_job_snapshot_state`) against the dark snapshot
#: helper. Snapshot submission journals and starts one detached fixed pvesh
#: runner; sealing performs no PVE mutation. The call this bounds therefore
#: never waits for local pvesh CLI's synchronous physical snapshot work.
#: The existing canonical timeout used by tests
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

#: Deliberate upper bound on `submission_timeout_seconds` for
#: `SshPackageUpdateMutationHostControl` -- the wall-clock budget for ONE
#: bounded SSH round trip running a typed operation that this backend calls
#: while it still holds the authority writer lock
#: (`execute_exact_package_mutation` or `seal_mutation_never_submitted`).
#: Neither waits for `apt-get` itself: the mutation helper journals
#: `submitted` and hands the real package command to a detached host runner,
#: so what this bounds is a live PVE target read, one bounded `dpkg-query`
#: pre-state read, two fsynced journal writes, and a fork -- never the
#: package mutation's own duration. The helper's own read-only operations
#: (`prepare_exact_package_mutation`, `inspect_package_mutation_state`) run
#: strictly OUTSIDE the writer lock and are deliberately NOT bounded by this
#: value: an APT metadata refresh plus simulation can legitimately take
#: minutes, which is exactly why it may never happen inside the lock.
MAX_PACKAGE_MUTATION_SUBMISSION_TIMEOUT_SECONDS = 90

#: Deliberate upper bound on `timeout_seconds` for
#: `SshPackageUpdateRollbackHostControl` -- the wall-clock budget for ONE
#: bounded SSH round trip running a typed operation that this backend calls
#: while it still holds the authority writer lock
#: (`submit_same_job_rollback` or `seal_rollback_never_submitted`). Submission
#: journals `submitted` and starts one detached fixed pvesh runner; local
#: pvesh CLI's synchronous physical rollback work remains outside the writer
#: transaction. What this bounds is live PVE validation, a canonical snapshot
#: read, fsynced journal work, and the detach boundary -- never the rollback's
#: own duration, which includes force-stopping the
#: container and replacing its volumes and config. The helper's read-only
#: `inspect_rollback_state` runs strictly OUTSIDE the writer lock and is
#: deliberately NOT bounded by this value.
MAX_ROLLBACK_SUBMISSION_TIMEOUT_SECONDS = 90

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
#: The worst-case wall-clock duration ONE package-mutation host critical
#: section (`execute_package_mutation_submission_if_current` or
#: `resolve_pre_mutation_block`) may legitimately hold the authority store's
#: writer lock. Same shape as the snapshot bound above, over the mutation
#: transport's own submission ceiling.
MAX_PACKAGE_MUTATION_CRITICAL_SECTION_SECONDS = (
    MAX_PACKAGE_MUTATION_SUBMISSION_TIMEOUT_SECONDS + BOUNDED_PROCESS_CLEANUP_SECONDS
)

#: The worst-case wall-clock duration ONE same-job rollback host critical
#: section (`execute_rollback_submission_if_current` or
#: `resolve_pre_rollback_block`) may legitimately hold the authority store's
#: writer lock. Same shape and same reason as the two bounds above, over the
#: rollback transport's own submission ceiling.
MAX_ROLLBACK_CRITICAL_SECTION_SECONDS = (
    MAX_ROLLBACK_SUBMISSION_TIMEOUT_SECONDS + BOUNDED_PROCESS_CLEANUP_SECONDS
)

#: The worst case over EVERY critical section that may legitimately hold the
#: authority store's writer lock across a bounded host round trip. The writer
#: wait budget is derived from this, not from the snapshot bound alone, so
#: adding a further such critical section can never silently leave ordinary
#: writers with a budget shorter than a healthy one of them.
MAX_HOST_CRITICAL_SECTION_SECONDS = max(
    MAX_SNAPSHOT_HOST_CRITICAL_SECTION_SECONDS,
    MAX_PACKAGE_MUTATION_CRITICAL_SECTION_SECONDS,
    MAX_ROLLBACK_CRITICAL_SECTION_SECONDS,
)

AUTHORITY_WRITER_WAIT_BUDGET_SECONDS = (
    MAX_HOST_CRITICAL_SECTION_SECONDS + WRITER_SCHEDULING_MARGIN_SECONDS
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

assert (
    AUTHORITY_WRITER_WAIT_BUDGET_SECONDS
    > MAX_PACKAGE_MUTATION_CRITICAL_SECTION_SECONDS
), (
    "authority writer wait budget must exceed the maximum bounded package "
    "mutation host critical-section duration plus its scheduling margin"
)

assert (
    AUTHORITY_WRITER_WAIT_BUDGET_SECONDS > MAX_ROLLBACK_CRITICAL_SECTION_SECONDS
), (
    "authority writer wait budget must exceed the maximum bounded same-job "
    "rollback host critical-section duration plus its scheduling margin"
)
