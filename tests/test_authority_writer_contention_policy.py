"""Real SQLite regressions for the authority writer-contention policy.

`InventoryAuthorityStore` used to size `PRAGMA busy_timeout` and its
connection `timeout` from one fixed `BUSY_TIMEOUT_MS = 5000` -- unrelated to
how long `InventoryAuthority.execute_snapshot_submission_if_current` and
`InventoryAuthority.resolve_pre_submission_block` may legitimately hold the
one writer lock across a bounded snapshot host round trip. This module proves
the fix against the actual SQLite database, not a mock: no `sqlite3` busy
handling is faked anywhere below.
"""

from __future__ import annotations

from pathlib import Path
import sqlite3
import threading
import time

import pytest

from app.inventory import InventoryAuthorityStore
from app.inventory.contention_policy import (
    AUTHORITY_WRITER_WAIT_BUDGET_MS,
    AUTHORITY_WRITER_WAIT_BUDGET_SECONDS,
    MAX_SNAPSHOT_HOST_CRITICAL_SECTION_SECONDS,
    MAX_SNAPSHOT_HOST_TIMEOUT_SECONDS,
)
from app.inventory.store import BUSY_TIMEOUT_MS
from app.package_update_snapshot_host_control import (
    SshPackageUpdateSnapshotHostControl,
)


# Old fixed budget PR #67 shipped with -- the concurrency regression below
# must run longer than this to be direct evidence against the actual bug,
# per the policy's own bounded validation gate.
_OLD_BUSY_TIMEOUT_SECONDS = 5.0

# How long the "healthy bounded host critical section" thread holds the
# writer lock in the tests below. Longer than the old budget, comfortably
# inside the new one, and short enough to keep the suite fast.
_HOLD_SECONDS = 6.0

_JOIN_TIMEOUT_SECONDS = 30.0


def _bump_revision(connection: sqlite3.Connection) -> None:
    connection.execute(
        "UPDATE backend_instance SET inventory_revision=inventory_revision+1"
    )


def test_a_healthy_over_five_second_snapshot_critical_section_does_not_starve_an_ordinary_writer(
    tmp_path: Path,
) -> None:
    """Direct witness against the previous 5-second liveness bug.

    Writer A holds `BEGIN IMMEDIATE` for longer than the old fixed budget,
    simulating one healthy bounded snapshot host critical section. Writer B
    starts an ordinary authority write while A still holds the lock. B must
    not fail with "database is locked"; it must wait, then complete once A
    releases, with the durable state reflecting both writes.
    """

    store = InventoryAuthorityStore(tmp_path / "authority.db")
    assert _HOLD_SECONDS > _OLD_BUSY_TIMEOUT_SECONDS
    assert AUTHORITY_WRITER_WAIT_BUDGET_SECONDS > _HOLD_SECONDS

    acquired = threading.Event()
    errors: list[BaseException] = []
    b_elapsed: list[float] = []

    def writer_a() -> None:
        try:
            with store._transaction() as connection:
                # BEGIN IMMEDIATE has already run by the time the context
                # manager yields -- A genuinely owns the writer lock here.
                acquired.set()
                time.sleep(_HOLD_SECONDS)
                _bump_revision(connection)
        except BaseException as exc:  # noqa: BLE001 - captured for the assertion
            errors.append(exc)

    def writer_b() -> None:
        assert acquired.wait(timeout=10), "writer A never acquired the lock"
        started = time.monotonic()
        try:
            with store._transaction() as connection:
                _bump_revision(connection)
        except BaseException as exc:  # noqa: BLE001 - captured for the assertion
            errors.append(exc)
        else:
            b_elapsed.append(time.monotonic() - started)

    thread_a = threading.Thread(target=writer_a)
    thread_b = threading.Thread(target=writer_b)
    thread_a.start()
    thread_b.start()
    thread_a.join(timeout=_JOIN_TIMEOUT_SECONDS)
    thread_b.join(timeout=_JOIN_TIMEOUT_SECONDS)

    assert not thread_a.is_alive()
    assert not thread_b.is_alive()
    assert errors == []
    # B actually waited on A's lock rather than racing ahead of it.
    assert b_elapsed and b_elapsed[0] >= _HOLD_SECONDS - 0.5
    # Both writes are durably reflected -- no lost update, no corruption.
    assert store.backend_instance().inventory_revision == 2


def test_writer_lock_releases_when_the_holding_connection_dies(
    tmp_path: Path,
) -> None:
    """Positive control: a dead holder is not a permanent deadlock.

    Writer A acquires `BEGIN IMMEDIATE` directly (bypassing the store's own
    commit/rollback bookkeeping) and its connection is then closed as though
    the process that held it had failed, never having committed or rolled
    back explicitly. Writer B, already waiting, must still proceed within its
    bounded wait and reach a correct durable state -- no manual repair.
    """

    store = InventoryAuthorityStore(tmp_path / "authority.db")

    acquired = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []
    b_elapsed: list[float] = []

    def crash_holder() -> None:
        connection = sqlite3.connect(
            store.path, timeout=BUSY_TIMEOUT_MS / 1_000, isolation_level=None
        )
        try:
            connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            connection.execute("BEGIN IMMEDIATE")
            _bump_revision(connection)
            acquired.set()
            assert release.wait(timeout=10), "test never signalled the crash"
            # Simulated crash: closed without COMMIT or ROLLBACK. SQLite
            # rolls back the open transaction and releases the writer lock
            # as part of closing the connection.
        finally:
            connection.close()

    def waiting_writer() -> None:
        assert acquired.wait(timeout=10), "holder never acquired the lock"
        started = time.monotonic()
        try:
            with store._transaction() as connection:
                _bump_revision(connection)
        except BaseException as exc:  # noqa: BLE001 - captured for the assertion
            errors.append(exc)
        else:
            b_elapsed.append(time.monotonic() - started)

    thread_a = threading.Thread(target=crash_holder)
    thread_b = threading.Thread(target=waiting_writer)
    thread_a.start()
    thread_b.start()
    # Give B a real chance to start blocking on A's held lock before A "dies".
    time.sleep(1.0)
    release.set()
    thread_a.join(timeout=_JOIN_TIMEOUT_SECONDS)
    thread_b.join(timeout=_JOIN_TIMEOUT_SECONDS)

    assert not thread_a.is_alive()
    assert not thread_b.is_alive()
    assert errors == []
    assert b_elapsed != []
    # A's uncommitted write was rolled back when its connection died -- only
    # B's write, made after A's lock was released, is durable.
    assert store.backend_instance().inventory_revision == 1


def _fail_if_invoked(argv, input_bytes, timeout, max_output):
    raise AssertionError(
        "host-control must reject an out-of-range timeout before any "
        "process execution"
    )


def _host_control(*, timeout_seconds: int, runner=_fail_if_invoked):
    return SshPackageUpdateSnapshotHostControl(
        host="pve.example.internal",
        port=22,
        user="hubinet-snapshot",
        private_key_path=Path("/etc/hubinet-ops/snapshot-key"),
        known_hosts_path=Path("/etc/hubinet-ops/known_hosts"),
        timeout_seconds=timeout_seconds,
        max_result_bytes=1024 * 1024,
        runner=runner,
    )


def test_a_normal_snapshot_host_timeout_is_accepted() -> None:
    # The canonical timeout used across the snapshot safety test suite.
    _host_control(timeout_seconds=60)


def test_the_maximum_allowed_snapshot_host_timeout_is_accepted() -> None:
    _host_control(timeout_seconds=MAX_SNAPSHOT_HOST_TIMEOUT_SECONDS)


def test_above_the_maximum_snapshot_host_timeout_is_rejected_before_execution() -> None:
    with pytest.raises(ValueError, match="host-control timeout"):
        _host_control(timeout_seconds=MAX_SNAPSHOT_HOST_TIMEOUT_SECONDS + 1)
    # The historical unrelated ceiling must not silently still be accepted.
    with pytest.raises(ValueError, match="host-control timeout"):
        _host_control(timeout_seconds=3600)


def test_the_writer_wait_budget_exceeds_the_maximum_host_critical_section() -> None:
    assert (
        AUTHORITY_WRITER_WAIT_BUDGET_SECONDS
        > MAX_SNAPSHOT_HOST_CRITICAL_SECTION_SECONDS
    )
    # Concretely: a legal maximal snapshot host round trip plus its bounded
    # process cleanup allowance still leaves real margin under the budget.
    assert (
        AUTHORITY_WRITER_WAIT_BUDGET_SECONDS
        - MAX_SNAPSHOT_HOST_CRITICAL_SECTION_SECONDS
        > 0
    )


def test_connection_busy_timeout_pragma_matches_the_policy_budget(
    tmp_path: Path,
) -> None:
    store = InventoryAuthorityStore(tmp_path / "authority.db")
    with store._read_connection() as connection:
        pragma_value = connection.execute("PRAGMA busy_timeout").fetchone()[0]
    assert pragma_value == BUSY_TIMEOUT_MS == AUTHORITY_WRITER_WAIT_BUDGET_MS


def test_python_connect_timeout_and_pragma_busy_timeout_share_one_source() -> None:
    # Both are derived from the same module constant in `store._connect` --
    # not two independently editable literals that could drift apart.
    assert BUSY_TIMEOUT_MS == AUTHORITY_WRITER_WAIT_BUDGET_MS
    assert BUSY_TIMEOUT_MS / 1_000 == AUTHORITY_WRITER_WAIT_BUDGET_SECONDS
