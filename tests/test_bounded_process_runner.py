"""Regression coverage for `_bounded_process_runner`'s stdin delivery.

`_bounded_process_runner` is the one subprocess primitive shared by every
typed host-control transport (package scan, package-update snapshot,
package-update execution, and package mutation). Its configured `timeout`
is relied on as a single wall-clock budget for the whole round trip --
including delivering the request on stdin -- by callers that hold an
authority writer transaction across the call (see
`app.inventory.contention_policy`). These tests exercise the runner
directly, against real local subprocesses, because a fake injected
`ProcessRunner` (as used elsewhere) cannot reproduce the actual pipe
blocking this bug is about.

Every test that could hang if the bug were reintroduced runs the runner on
a background daemon thread and joins with a bounded timeout, so a
regression fails the test instead of hanging the suite.
"""

from __future__ import annotations

import sys
import threading
import time

from app.inventory import BOUNDED_PROCESS_CLEANUP_SECONDS
from app.package_scan_host_control import _bounded_process_runner


# Comfortably larger than any OS pipe buffer (Linux defaults to 64 KiB), so
# a blocking write would stall until the reader (or a timeout) intervenes.
_LARGE_INPUT = b"x" * (2 * 1024 * 1024)

# A hard ceiling on how long a call is allowed to occupy the test's join,
# independent of the runner's own timeout: this is what turns "the bug
# reintroduces an infinite block" into a failing assertion instead of a
# hung test process.
_JOIN_TIMEOUT = 20.0


def _run_bounded(argv, input_bytes, timeout, max_output):
    """Run `_bounded_process_runner` on a daemon thread, bounded by a join.

    Returns `(result_or_none, elapsed_seconds, hung)`.
    """

    box: dict[str, object] = {}
    started = time.monotonic()

    def target() -> None:
        box["result"] = _bounded_process_runner(argv, input_bytes, timeout, max_output)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout=_JOIN_TIMEOUT)
    elapsed = time.monotonic() - started
    if thread.is_alive():
        return None, elapsed, True
    return box.get("result"), elapsed, False


def _python(code: str) -> tuple[str, ...]:
    return (sys.executable, "-c", code)


def test_large_stdin_to_a_child_that_never_reads_it_times_out_bounded() -> None:
    """A. The deadline covers stdin delivery, not just output collection.

    Before the fix, `process.stdin.write(input_bytes)` ran to completion
    (or blocked forever) BEFORE the timeout clock started. A child that
    never drains stdin and outlives the configured timeout must still be
    killed within the configured bound, never left blocking the caller
    indefinitely.
    """

    argv = _python("import time\ntime.sleep(30)\n")
    result, elapsed, hung = _run_bounded(argv, _LARGE_INPUT, timeout=0.3, max_output=65536)
    assert not hung, "runner blocked past its configured timeout budget"
    assert result is not None
    assert result.timed_out is True
    assert elapsed < 0.3 + BOUNDED_PROCESS_CLEANUP_SECONDS + 5.0


def test_large_stdin_to_a_slow_consumer_is_delivered_completely() -> None:
    """B. Partial/non-blocking writes still deliver the whole request.

    A child that only ever reads small chunks, with a short sleep between
    reads, forces the runner through more than one non-blocking write.
    """

    child = (
        "import sys, time\n"
        "data = bytearray()\n"
        "while True:\n"
        "    chunk = sys.stdin.buffer.read(4096)\n"
        "    if not chunk:\n"
        "        break\n"
        "    data.extend(chunk)\n"
        "    time.sleep(0.001)\n"
        "sys.stdout.write(str(len(data)))\n"
    )
    result, elapsed, hung = _run_bounded(
        _python(child), _LARGE_INPUT, timeout=15.0, max_output=1024
    )
    assert not hung
    assert result is not None
    assert result.timed_out is False
    assert result.returncode == 0
    assert int(result.stdout) == len(_LARGE_INPUT)
    assert elapsed < 15.0


def test_child_exiting_before_reading_stdin_is_a_bounded_clean_result() -> None:
    """C. A child refusing stdin is a bounded failure, not a deadlock."""

    result, elapsed, hung = _run_bounded(
        _python("pass\n"), _LARGE_INPUT, timeout=10.0, max_output=65536
    )
    assert not hung
    assert result is not None
    assert result.timed_out is False
    assert result.returncode == 0
    # The child exits almost instantly; this proves the runner does not
    # wait around for stdin delivery that can never happen.
    assert elapsed < 5.0


def test_large_stdin_with_concurrent_stdout_and_stderr_does_not_deadlock() -> None:
    """D. Writing stdin and reading two pipes at once stays bounded and correct."""

    child = (
        "import sys\n"
        "total = 0\n"
        "while True:\n"
        "    chunk = sys.stdin.buffer.read(4096)\n"
        "    if not chunk:\n"
        "        break\n"
        "    total += len(chunk)\n"
        "    sys.stdout.buffer.write(b'o' * 100)\n"
        "    sys.stdout.buffer.flush()\n"
        "    sys.stderr.buffer.write(b'e' * 100)\n"
        "    sys.stderr.buffer.flush()\n"
        "sys.stdout.buffer.write(str(total).encode())\n"
        "sys.stdout.buffer.flush()\n"
    )
    input_bytes = b"y" * (300 * 1024)
    result, elapsed, hung = _run_bounded(
        _python(child), input_bytes, timeout=15.0, max_output=2_000_000
    )
    assert not hung
    assert result is not None
    assert result.timed_out is False
    assert result.output_exceeded is False
    assert result.returncode == 0
    assert result.stdout.endswith(str(len(input_bytes)).encode())
    assert len(result.stderr) > 0
    assert elapsed < 15.0


def test_timeout_is_one_total_budget_not_reset_after_stdin_completes() -> None:
    """E. Fully delivering stdin quickly must not grant a fresh timeout window."""

    child = "import sys, time\nsys.stdin.buffer.read()\ntime.sleep(30)\n"
    small_input = b"y" * 1024
    result, elapsed, hung = _run_bounded(
        _python(child), small_input, timeout=0.3, max_output=65536
    )
    assert not hung
    assert result is not None
    assert result.timed_out is True
    assert elapsed < 0.3 + BOUNDED_PROCESS_CLEANUP_SECONDS + 5.0


def test_small_input_round_trip_still_works() -> None:
    """Positive control: ordinary small-input echo behavior is unchanged."""

    child = "import sys\nsys.stdout.buffer.write(sys.stdin.buffer.read())\n"
    input_bytes = b'{"operation": "scan_packages"}'
    result, elapsed, hung = _run_bounded(
        _python(child), input_bytes, timeout=5.0, max_output=65536
    )
    assert not hung
    assert result is not None
    assert result.timed_out is False
    assert result.returncode == 0
    assert result.stdout == input_bytes
    assert elapsed < 5.0


def test_empty_input_closes_stdin_immediately() -> None:
    """No request bytes still closes stdin promptly so the child sees EOF."""

    child = "import sys\nsys.stdout.write(str(len(sys.stdin.buffer.read())))\n"
    result, elapsed, hung = _run_bounded(_python(child), b"", timeout=5.0, max_output=65536)
    assert not hung
    assert result is not None
    assert result.timed_out is False
    assert result.returncode == 0
    assert result.stdout == b"0"
    assert elapsed < 5.0
