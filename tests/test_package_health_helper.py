"""The dark health helper's fixed argv, bounded settling, and every way it
must not false-PASS.

The helper is the one place in this repository where an operator-supplied
string becomes part of a command line, so this file is deliberately adversarial
about it. It drives the REAL helper module against a fake guest that answers
exactly the fixed argv shapes the helper issues -- an unrecognised command is a
test failure, not a silent empty result -- and it asserts the argv itself, not
only the verdict.

The CLI behaviours these tests encode were verified against the real tool
(systemd 257) rather than assumed; `ARCHITECTURE.md`, "Job-bound healthcheck
execution", records what was observed. In particular:

- `systemctl is-active` expands globs and succeeds if ANY match is active,
  and `--` does not stop it -- which is why this helper does not use it;
- a systemd glob can match exactly ONE unit, so "one property block" alone is
  not enough and the target charset must exclude `*`, `?`, `[`;
- `ssh.service` and `sshd.service` are aliases and report the SAME `Id`, so
  batched systemd results are mapped BY POSITION, never by `Id`.

v0.5 dropped Docker-specific package-update health probes entirely --
`docker_container_running` and `docker_container_healthy` no longer exist, so
there is no Docker behaviour left for this file to encode.

Every scenario below runs through a fake, instantly-advancing clock: the
advanced `systemd_unit_active` settling loop requires at least
`MIN_DECISIVE_ROUND` (2) rounds before any verdict, so `_evaluate` always
injects a deterministic clock rather than sleeping for real. The built-in
`guest_operational` baseline is a single one-shot execution and never sleeps
at all (see section 2 below). Nothing here runs a real `pvesh`, `pct`, `ssh`,
or `systemctl`.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType
import uuid

import pytest


ROOT = Path(__file__).resolve().parents[1]
HELPER_PATH = ROOT / "deploy" / "hubinet-package-health-helper.py"

NODE = "pve-a"
VMID = 110


def _load_helper() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "hubinet_package_health_helper_hermetic", HELPER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


helper = _load_helper()


# ===========================================================================
# A deterministic clock: bounded settling needs >= 2 rounds before any
# verdict, so tests never sleep for real.
# ===========================================================================


class FakeClock:
    """``monotonic`` ticks a hair on every call (realistic small deltas, so a
    round's own span stays well under ``MAX_ROUND_SPAN_SECONDS``); ``sleep``
    jumps the same clock forward by the requested amount, so the bounded
    deadline is real virtual time without any real waiting."""

    def __init__(self) -> None:
        self.t = 0.0

    def monotonic(self) -> float:
        self.t += 0.001
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


# ===========================================================================
# A fake guest, driven through the REAL dark helper
# ===========================================================================


class FakeGuest:
    """A deterministic stand-in for one running Debian LXC guest.

    Its systemd behaviour models what the real tool was OBSERVED to do,
    including the parts that make a naive probe unsafe: `systemctl show`
    emits one blank-line-separated property block per matched unit and
    expands `*`, `?`, `[`.
    """

    def __init__(self) -> None:
        self.vmid = VMID
        self.node = NODE
        self.present = True
        self.running = True
        self.resource_type = "lxc"
        self.current_node = NODE
        #: unit id -> (LoadState, ActiveState, Job). `nginx.service` and
        #: `postgresql.service` are both active by default so the default
        #: two-probe advanced contract (`tests.test_package_update_job_
        #: authority.HEALTH_PROBES`) passes with no extra configuration.
        self.units: dict[str, tuple[str, str, str]] = {
            "nginx.service": ("loaded", "active", ""),
            "postgresql.service": ("loaded", "active", ""),
            "worker.service": ("loaded", "failed", ""),
        }
        #: Alias -> canonical unit id, exactly as systemd resolves one.
        self.unit_aliases: dict[str, str] = {}
        self.systemctl_returncode = 0
        self.systemctl_stdout_override: str | None = None
        self.commands: list[tuple[str, ...]] = []
        self.timeout_on: str | None = None
        self.guest_operational_ok = True
        #: PR #80 review finding 1 regression support. A shared clock this
        #: fake advances by (up to) the granted `timeout` before answering
        #: call number N (0-based) -- modelling a subprocess that genuinely
        #: takes real wall-clock time to answer, rather than the always-
        #: instant default that could never expose stale-remaining-budget
        #: reuse. Consumption is capped at the timeout actually granted: a
        #: real bounded subprocess cannot run longer than its own timeout.
        self.clock: FakeClock | None = None
        self.consume_seconds_by_call_index: dict[int, float] = {}
        self.call_index = 0
        self.call_timeouts: list[float] = []

    # -- host commands -------------------------------------------------

    def __call__(self, argv, timeout, max_output):
        self.commands.append(tuple(argv))
        self.call_timeouts.append(timeout)
        requested_consumption = self.consume_seconds_by_call_index.get(
            self.call_index, 0.0
        )
        if requested_consumption and self.clock is not None:
            self.clock.t += min(requested_consumption, timeout)
        self.call_index += 1
        if argv[:2] == ("pvesh", "get") and argv[2] == "/cluster/status":
            return self._ok(
                json.dumps(
                    [{"type": "node", "name": self.node, "local": 1}]
                ).encode()
            )
        if argv[:2] == ("pvesh", "get") and argv[2] == "/cluster/resources":
            rows = []
            if self.present:
                rows.append(
                    {
                        "vmid": self.vmid,
                        "type": self.resource_type,
                        "node": self.current_node,
                        "status": "running" if self.running else "stopped",
                    }
                )
            return self._ok(json.dumps(rows).encode())
        if argv[:2] == ("pct", "exec"):
            return self._guest(argv[4:])
        raise AssertionError(f"unexpected host command: {argv}")

    def _guest(self, tail):
        if tail == helper.GUEST_OPERATIONAL_COMMAND:
            return self._guest_operational(tail)
        assert tail[0] == "env" and tail[1] == "LC_ALL=C", tail
        command = tail[2]
        if command == "systemctl":
            return self._systemctl(tail)
        raise AssertionError(f"unexpected guest command: {tail}")

    # -- guest_operational -----------------------------------------------

    def _guest_operational(self, tail):
        assert tail == ("/bin/true",), tail
        if self.timeout_on == "guest_operational":
            return helper.CommandResult(0, b"", b"", timed_out=True)
        return helper.CommandResult(
            0 if self.guest_operational_ok else 1, b"", b""
        )

    # -- systemd -------------------------------------------------------

    def _systemctl(self, tail):
        assert tail[3] == "show", tail
        assert "--no-pager" in tail, tail
        assert "--property=Job" in tail, tail
        # Everything after the end-of-options marker is a unit name, never an
        # option.
        assert "--" in tail, tail
        assert "is-active" not in tail, "is-active can pass on another unit"
        requested = tail[tail.index("--") + 1 :]
        if self.timeout_on == "systemctl":
            return helper.CommandResult(0, b"", b"", timed_out=True)
        if self.systemctl_stdout_override is not None:
            return helper.CommandResult(
                self.systemctl_returncode,
                self.systemctl_stdout_override.encode(),
                b"",
            )
        if self.systemctl_returncode != 0:
            return helper.CommandResult(self.systemctl_returncode, b"", b"boom")
        blocks = []
        for one in requested:
            matched = self._match_units(one)
            if not matched:
                # systemd answers for a unit that does not exist with a
                # normal success and LoadState=not-found.
                blocks.append(
                    f"Id={one}\nLoadState=not-found\nActiveState=inactive\nJob="
                )
                continue
            for unit in matched:
                load_state, active_state, job = self.units[unit]
                blocks.append(
                    f"Id={unit}\nLoadState={load_state}\n"
                    f"ActiveState={active_state}\nJob={job}"
                )
        return self._ok(("\n\n".join(blocks) + "\n").encode())

    def _match_units(self, requested: str) -> list[str]:
        import fnmatch

        if any(character in requested for character in "*?["):
            return sorted(
                unit for unit in self.units if fnmatch.fnmatchcase(unit, requested)
            )
        canonical = self.unit_aliases.get(requested, requested)
        return [canonical] if canonical in self.units else []

    # -- helpers -------------------------------------------------------

    @staticmethod
    def _ok(stdout: bytes):
        return helper.CommandResult(0, stdout, b"")


def _request(probes, *, vmid: int = VMID, node: str = NODE) -> dict:
    return {
        "request_version": 1,
        "operation": "evaluate_health_contract",
        "target": {"vmid": vmid, "expected_node": node},
        "ownership": {
            "job_id": str(uuid.uuid4()),
            "resource_id": str(uuid.uuid4()),
            "resource_continuity_revision": 1,
            "binding_id": str(uuid.uuid4()),
            "locator_generation": 1,
            "backend_instance_id": str(uuid.uuid4()),
        },
        "health_contract": {
            "revision": 4,
            "fingerprint": "a" * 64,
            "probes": [
                {"index": index, "kind": kind, "target": target}
                for index, (kind, target) in enumerate(probes)
            ],
        },
        "settling_policy": {
            "deadline_seconds": helper.DEFAULT_SETTLING_DEADLINE_SECONDS,
            "observation_interval_seconds": (
                helper.DEFAULT_OBSERVATION_INTERVAL_SECONDS
            ),
        },
    }


def _evaluate(guest, probes, *, node: str = NODE, clock: FakeClock | None = None):
    clock = clock or FakeClock()
    response = helper.handle_request(
        _request(probes, node=node),
        runner=guest,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    assert response["ok"] is True, response
    assert response["health_contract"] == {"revision": 4, "fingerprint": "a" * 64}
    return [(probe["outcome"], probe["reason"]) for probe in response["probes"]]


def _evaluate_full(guest, probes, *, node: str = NODE, clock: FakeClock | None = None):
    """Like :func:`_evaluate`, but returns the whole response for settling
    metadata assertions."""

    clock = clock or FakeClock()
    response = helper.handle_request(
        _request(probes, node=node),
        runner=guest,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    assert response["ok"] is True, response
    return response


# ===========================================================================
# 1. systemd_unit_active
# ===========================================================================


def test_an_active_unit_passes_with_the_exact_fixed_argv() -> None:
    guest = FakeGuest()
    assert _evaluate(guest, (("systemd_unit_active", "nginx.service"),)) == [
        ("passed", "unit_active")
    ]
    guest_command = next(
        argv for argv in guest.commands if argv[:2] == ("pct", "exec")
    )
    assert guest_command == (
        "pct",
        "exec",
        str(VMID),
        "--",
        "env",
        "LC_ALL=C",
        "systemctl",
        "show",
        "--no-pager",
        "--property=Id",
        "--property=LoadState",
        "--property=ActiveState",
        "--property=Job",
        "--",
        "nginx.service",
    )


def test_a_known_inactive_unit_with_no_pending_job_is_a_definitive_failure() -> None:
    guest = FakeGuest()
    assert _evaluate(guest, (("systemd_unit_active", "worker.service"),)) == [
        ("failed", "unit_not_active")
    ]


def test_a_unit_that_does_not_exist_is_a_definitive_failure() -> None:
    """systemd reports it as a normal success with ActiveState=inactive, and
    a unit that is not there is definitively not active."""

    guest = FakeGuest()
    assert _evaluate(guest, (("systemd_unit_active", "absent.service"),)) == [
        ("failed", "unit_not_active")
    ]


@pytest.mark.parametrize(
    ("active_state", "reason"),
    (
        ("activating", "unit_activating"),
        ("deactivating", "unit_deactivating"),
        ("reloading", "unit_reloading"),
    ),
)
def test_a_unit_mid_transition_is_unknown_not_a_verdict(
    active_state: str, reason: str
) -> None:
    """systemd's own transient job states are never a workload verdict --
    they resolve on their own within a bounded settling window."""

    guest = FakeGuest()
    guest.units["nginx.service"] = ("loaded", active_state, "")
    assert _evaluate(guest, (("systemd_unit_active", "nginx.service"),)) == [
        ("unknown", reason)
    ]


@pytest.mark.parametrize("active_state", ("inactive", "failed"))
def test_an_inactive_or_failed_unit_with_a_pending_job_is_unknown(
    active_state: str,
) -> None:
    """``Job`` distinguishes a unit still settling from one genuinely at
    rest: `systemctl show --property=Job` is empty while idle and numeric
    mid-transition (verified against systemd 257)."""

    guest = FakeGuest()
    guest.units["nginx.service"] = ("loaded", active_state, "12345")
    assert _evaluate(guest, (("systemd_unit_active", "nginx.service"),)) == [
        ("unknown", "unit_job_pending")
    ]


def test_maintenance_is_a_definitive_failure() -> None:
    guest = FakeGuest()
    guest.units["nginx.service"] = ("loaded", "maintenance", "")
    assert _evaluate(guest, (("systemd_unit_active", "nginx.service"),)) == [
        ("failed", "unit_not_active")
    ]


@pytest.mark.parametrize(
    "target",
    (
        "nginx*",
        "*.service",
        "nginx?service",
        "nginx[.]service",
        "ngin[x].service",
    ),
)
def test_a_glob_target_can_never_produce_a_pass(target: str) -> None:
    """The false PASS this whole design exists to prevent.

    Every one of these would match `nginx.service` through systemd's own
    pattern expansion -- including the two that match EXACTLY ONE unit, which
    is why "one property block" alone is not a sufficient defence.
    """

    guest = FakeGuest()
    assert _evaluate(guest, (("systemd_unit_active", target),)) == [
        ("unknown", "probe_target_not_exact")
    ]
    # It never even reached the guest: structural target rejection is
    # round-independent and spends no guest command.
    assert not any(argv[:2] == ("pct", "exec") for argv in guest.commands)


@pytest.mark.parametrize("target", ("--help", "-H", "--all"))
def test_an_option_like_unit_target_can_never_become_an_option(target: str) -> None:
    guest = FakeGuest()
    assert _evaluate(guest, (("systemd_unit_active", target),)) == [
        ("unknown", "probe_target_not_exact")
    ]


def test_a_unit_without_an_explicit_type_suffix_is_not_exact() -> None:
    """`systemctl show nginx` silently resolves to `nginx.service`. Deciding
    on the operator's behalf which object they meant would be broadening the
    contract, so it is reported unevaluable instead."""

    guest = FakeGuest()
    assert _evaluate(guest, (("systemd_unit_active", "nginx"),)) == [
        ("unknown", "probe_target_not_exact")
    ]


def test_a_unit_alias_resolves_to_its_canonical_unit_and_may_pass() -> None:
    """An alias IS the unit, not a pattern matching it, so this is exact."""

    guest = FakeGuest()
    guest.unit_aliases["httpd.service"] = "nginx.service"
    assert _evaluate(guest, (("systemd_unit_active", "httpd.service"),)) == [
        ("passed", "unit_active")
    ]


def test_two_aliased_targets_are_mapped_by_position_never_by_id() -> None:
    """`ssh.service` and `sshd.service` are ALIASES of the same unit and
    report the identical `Id` (verified against systemd 257): only the
    batched call's own request order can tell two such targets apart."""

    guest = FakeGuest()
    guest.units["ssh.service"] = ("loaded", "active", "")
    guest.unit_aliases["sshd.service"] = "ssh.service"
    guest.units["cron.service"] = ("loaded", "failed", "")
    assert _evaluate(
        guest,
        (
            ("systemd_unit_active", "ssh.service"),
            ("systemd_unit_active", "cron.service"),
            ("systemd_unit_active", "sshd.service"),
        ),
    ) == [
        ("passed", "unit_active"),
        ("failed", "unit_not_active"),
        ("passed", "unit_active"),
    ]
    # ONE batched systemctl show for all three targets, not three commands.
    systemctl_calls = [
        argv for argv in guest.commands if argv[:2] == ("pct", "exec") and "systemctl" in argv
    ]
    assert len(systemctl_calls) == 2  # two decisive rounds, one command each


def test_a_mismatched_block_count_is_ambiguous_never_a_pass() -> None:
    """Belt and braces under the charset check: if the batched answer ever
    returned more blocks than requested targets, positional mapping is no
    longer trustworthy for ANY of them."""

    guest = FakeGuest()
    guest.systemctl_stdout_override = (
        "Id=nginx.service\nLoadState=loaded\nActiveState=active\nJob=\n\n"
        "Id=other.service\nLoadState=loaded\nActiveState=active\nJob=\n"
    )
    assert _evaluate(guest, (("systemd_unit_active", "nginx.service"),)) == [
        ("unknown", "probe_target_ambiguous")
    ]


@pytest.mark.parametrize(
    "stdout",
    (
        "",
        "ActiveState=active\nJob=\n",
        "Id=nginx.service\nActiveState=active\nJob=\n",
        "Id=nginx.service\nLoadState=loaded\nActiveState=active\nJob=\nExtra=1\n",
        "Id=nginx.service\nLoadState=loaded\nActiveState=quantum\nJob=\n",
        "not even a property line\n",
    ),
)
def test_malformed_systemctl_output_is_unknown_never_a_pass(stdout: str) -> None:
    guest = FakeGuest()
    guest.systemctl_stdout_override = stdout
    outcome, _ = _evaluate(guest, (("systemd_unit_active", "nginx.service"),))[0]
    assert outcome == "unknown"


def test_a_failed_systemctl_command_is_unknown_never_a_verdict() -> None:
    """No systemd in the guest, a broken bus, a permission problem. "The
    command ran" is never a PASS, and "it did not" is never a FAIL."""

    guest = FakeGuest()
    guest.systemctl_returncode = 1
    assert _evaluate(guest, (("systemd_unit_active", "nginx.service"),)) == [
        ("unknown", "command_failed")
    ]


def test_a_systemctl_timeout_is_unknown() -> None:
    guest = FakeGuest()
    guest.timeout_on = "systemctl"
    assert _evaluate(guest, (("systemd_unit_active", "nginx.service"),)) == [
        ("unknown", "command_timed_out")
    ]


# ===========================================================================
# 2. guest_operational: the v0.5 DEFAULT, and guest liveness only --
#    never application health. ONE-SHOT: no settling, no retry within one
#    evaluation attempt (v0.5 health scope reduction -- see ARCHITECTURE.md,
#    "Job-bound healthcheck execution -- baseline independence").
# ===========================================================================


def test_guest_operational_passes_with_the_exact_fixed_argv() -> None:
    guest = FakeGuest()
    assert _evaluate(guest, (("guest_operational", None),)) == [
        ("passed", "guest_operational_confirmed")
    ]
    guest_command = next(
        argv for argv in guest.commands if argv[:2] == ("pct", "exec")
    )
    assert guest_command == ("pct", "exec", str(VMID), "--", "/bin/true")
    # No operator-supplied argument crosses this boundary at all.
    assert "web" not in guest_command
    assert "nginx.service" not in guest_command


def test_guest_operational_never_carries_a_request_target() -> None:
    """Structural: the request validator refuses a target on this kind
    outright, before any guest command is even considered."""

    payload = _request((("guest_operational", None),))
    payload["health_contract"]["probes"][0]["target"] = "should-not-exist"
    with pytest.raises(helper.RequestError, match="must not carry a target"):
        helper.validate_request(payload)


def test_a_failed_guest_operation_is_unknown_never_a_definitive_failure() -> None:
    """No FAIL outcome exists for this kind at all: infrastructure
    uncertainty about a guest this stage cannot positively prove down is
    never turned into a failure of a workload this kind does not name."""

    guest = FakeGuest()
    guest.guest_operational_ok = False
    assert _evaluate(guest, (("guest_operational", None),)) == [
        ("unknown", "command_failed")
    ]


def test_a_guest_operation_timeout_is_unknown() -> None:
    guest = FakeGuest()
    guest.timeout_on = "guest_operational"
    assert _evaluate(guest, (("guest_operational", None),)) == [
        ("unknown", "command_timed_out")
    ]


def test_guest_operational_still_revalidates_the_live_target_first() -> None:
    guest = FakeGuest()
    guest.running = False
    clock = FakeClock()
    response = helper.handle_request(
        _request((("guest_operational", None),)),
        runner=guest,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    assert response["ok"] is False
    assert response["error"]["classification"] == "guest_unavailable"


def test_guest_operational_cannot_mix_with_a_systemd_probe() -> None:
    """The built-in baseline and an explicit advanced contract never mix
    (v0.5 health scope reduction, PRODUCT.md "What healthy means"): an
    advanced contract REPLACES the baseline, it does not extend it. Refused
    structurally at request validation, before any guest command runs."""

    payload = _request(
        (
            ("guest_operational", None),
            ("systemd_unit_active", "nginx.service"),
        )
    )
    with pytest.raises(helper.RequestError, match="guest_operational"):
        helper.validate_request(payload)


def test_guest_operational_executes_exactly_once_per_attempt() -> None:
    """No internal retry loop of any kind for the baseline: one attempt is
    one guest command, whatever the outcome."""

    guest = FakeGuest()
    _evaluate(guest, (("guest_operational", None),))
    guest_commands = [argv for argv in guest.commands if argv[:2] == ("pct", "exec")]
    assert len(guest_commands) == 1


def test_a_slow_but_successful_guest_operation_is_still_one_decisive_pass() -> None:
    """THE MAJOR FIX (v0.5 health scope reduction). At the starting SHA,
    `evaluate_health_contract_settling`'s generic advanced-settling rules
    (`MAX_ROUND_SPAN_SECONDS = 15`, `MIN_DECISIVE_ROUND = 2`) were incorrectly
    applied to a `guest_operational`-only contract: a `/bin/true` that
    genuinely took longer than 15 seconds to exit 0 was discarded as
    "non-decisive", and the evaluation kept re-running the command until the
    deadline, ending UNKNOWN with a stuck ACTIVE job -- for a workload that
    never actually failed.

    The baseline is now structurally independent of that settling machinery
    (`_evaluate_guest_operational_once`, called directly by `handle_request`,
    never `evaluate_health_contract_settling`): a single execution that
    exits 0 after MORE than `MAX_ROUND_SPAN_SECONDS` -- but comfortably
    inside the settling deadline -- is DECISIVE PASS from exactly ONE guest
    command, no sleep, and no second confirmation round.
    """

    guest = FakeGuest()
    clock = FakeClock()
    guest.clock = clock
    slow_seconds = helper.MAX_ROUND_SPAN_SECONDS + 1.0
    # Host call order for one guest_operational attempt: 0 = `_local_node`,
    # 1 = the prologue's own `revalidate_live_target`, 2 = the SECOND
    # revalidation `_run_guest_command` issues immediately before its own
    # guest command, 3 = the real `/bin/true` guest command itself. Consuming
    # the slow duration there proves a genuinely slow, but successful,
    # `/bin/true` alone -- not a slow revalidation -- is what stays PASS.
    guest.consume_seconds_by_call_index[3] = slow_seconds
    response = helper.handle_request(
        _request((("guest_operational", None),)),
        runner=guest,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    assert response["ok"] is True
    assert response["evaluation_status"] == "decisive"
    assert response["probes"] == [
        {
            "index": 0,
            "kind": "guest_operational",
            "target": None,
            "outcome": "passed",
            "reason": "guest_operational_confirmed",
        }
    ]
    assert response["settling"]["rounds"] == 1
    guest_commands = [argv for argv in guest.commands if argv[:2] == ("pct", "exec")]
    assert len(guest_commands) == 1
    assert guest_commands[0] == ("pct", "exec", str(VMID), "--", "/bin/true")


# ===========================================================================
# 3. Bounded settling: full-round temporal coherence and decisive rounds.
#    ONLY reachable for an advanced `systemd_unit_active` contract -- the
#    `guest_operational` baseline never enters this loop at all (section 2
#    above).
# ===========================================================================


def test_round_one_can_never_produce_a_verdict() -> None:
    """`MIN_DECISIVE_ROUND` refuses PASS or FAIL from the very first round,
    however clean it looks: it runs immediately after a package mutation that
    may itself still be settling."""

    calls = {"n": 0}
    guest = FakeGuest()
    original = guest._systemctl

    def counting(tail):
        calls["n"] += 1
        return original(tail)

    guest._systemctl = counting
    _evaluate(guest, (("systemd_unit_active", "nginx.service"),))
    # Two systemctl calls means two rounds ran before the (all-PASS) verdict.
    assert calls["n"] == 2


def test_full_round_temporal_coherence_earlier_pass_never_survives_a_later_fail() -> None:
    """The required counterexample regression: round 1 has A=PASS, B=
    TRANSIENT; round 2 has A=FAIL, B=PASS. The result must never be PASS --
    an earlier PASS contributes no terminal authority once a later round
    disproves it, and only the LAST complete round's evidence may decide.

    Both probes are `systemd_unit_active`, batched into one `systemctl show`
    call per round -- Docker health probes no longer exist (v0.5 health
    scope reduction), so this is now the only remaining targeted kind."""

    guest = FakeGuest()
    guest.units["nginx.service"] = ("loaded", "active", "")  # A: PASS round 1
    guest.units["cache.service"] = ("loaded", "activating", "")  # B: TRANSIENT

    calls = {"n": 0}
    original_systemctl = guest._systemctl

    def flipping(tail):
        calls["n"] += 1
        if calls["n"] >= 2:
            # Round 2 onward: A now fails definitively, B settles to PASS.
            guest.units["nginx.service"] = ("loaded", "failed", "")
            guest.units["cache.service"] = ("loaded", "active", "")
        return original_systemctl(tail)

    guest._systemctl = flipping

    response = _evaluate_full(
        guest,
        (
            ("systemd_unit_active", "nginx.service"),
            ("systemd_unit_active", "cache.service"),
        ),
    )
    outcomes = {probe["index"]: probe["outcome"] for probe in response["probes"]}
    assert outcomes[0] == "failed"
    assert response["probes"][0]["reason"] == "unit_not_active"
    # The whole contract is FAILED, never PASSED, because of it.
    assert any(probe["outcome"] == "failed" for probe in response["probes"])


def test_transient_settles_to_pass_within_the_bounded_window() -> None:
    guest = FakeGuest()
    guest.units["nginx.service"] = ("loaded", "activating", "")
    calls = {"n": 0}
    original = guest._systemctl

    def settling(tail):
        calls["n"] += 1
        if calls["n"] >= 3:
            guest.units["nginx.service"] = ("loaded", "active", "")
        return original(tail)

    guest._systemctl = settling
    assert _evaluate(guest, (("systemd_unit_active", "nginx.service"),)) == [
        ("passed", "unit_active")
    ]


def test_transient_settles_to_a_definitive_failure() -> None:
    guest = FakeGuest()
    guest.units["nginx.service"] = ("loaded", "activating", "")
    calls = {"n": 0}
    original = guest._systemctl

    def settling(tail):
        calls["n"] += 1
        if calls["n"] >= 3:
            guest.units["nginx.service"] = ("loaded", "failed", "")
        return original(tail)

    guest._systemctl = settling
    assert _evaluate(guest, (("systemd_unit_active", "nginx.service"),)) == [
        ("failed", "unit_not_active")
    ]


def test_persistent_transient_exhausts_the_deadline_as_unknown() -> None:
    """A workload that never settles either way reports UNKNOWN once the
    bounded structural round cap is reached, carrying only the LAST complete
    round's evidence, never a merged or partial one."""

    guest = FakeGuest()
    clock = FakeClock()
    guest.units["nginx.service"] = ("loaded", "activating", "")
    response = _evaluate_full(
        guest, (("systemd_unit_active", "nginx.service"),), clock=clock
    )
    assert response["probes"] == [
        {
            "index": 0,
            "kind": "systemd_unit_active",
            "target": "nginx.service",
            "outcome": "unknown",
            "reason": "unit_activating",
        }
    ]
    # The wall-clock deadline (180s / 5s observation interval) is reached
    # before the structural MAX_SETTLING_ROUNDS=40 cap ever would be. The
    # absolute deadline is checked BEFORE starting a new round, so the last
    # round that actually ran is the one just before cumulative sleep time
    # would reach the deadline.
    expected_rounds = int(
        helper.DEFAULT_SETTLING_DEADLINE_SECONDS
        // helper.DEFAULT_OBSERVATION_INTERVAL_SECONDS
    )
    assert response["settling"]["rounds"] == expected_rounds
    assert expected_rounds < helper.MAX_SETTLING_ROUNDS
    # The settling window ran right up to the shared absolute deadline: it
    # spends whatever the prologue (`_local_node`, the first live-target
    # revalidation) did NOT, rather than starting a fresh 180s of its own
    # (PR #80 review MINOR-1). So it gets very nearly the whole deadline,
    # and never more than it.
    assert (
        helper.DEFAULT_SETTLING_DEADLINE_SECONDS - 1.0
        <= response["settling"]["settled_seconds"]
        <= helper.DEFAULT_SETTLING_DEADLINE_SECONDS
    )
    # And the property that actually matters to the 300s outer transport:
    # the TOTAL operation -- prologue included -- stayed inside the
    # requested deadline. Before the fix this was "prologue + deadline".
    # (+0.1s covers only FakeClock's own 1ms-per-read ticks spent ASSEMBLING
    # the response after the loop broke -- no subprocess runs after the
    # deadline.)
    assert clock.t <= helper.DEFAULT_SETTLING_DEADLINE_SECONDS + 0.1
    assert response["settling"]["last_round_span_ms"] >= 0


def test_a_structural_target_problem_returns_immediately() -> None:
    """A target that can never settle is fixed forever, not host-dependent:
    it must never wait out the settling deadline, since no amount of time
    can ever make a structurally broken target resolve."""

    guest = FakeGuest()
    response = _evaluate_full(guest, (("systemd_unit_active", "nginx*"),))
    assert response["probes"][0]["outcome"] == "unknown"
    assert response["probes"][0]["reason"] == "probe_target_not_exact"
    assert response["evaluation_status"] == "unresolved"
    # No guest command was EVER spent on the structurally-broken target, and
    # the evaluation returned immediately rather than waiting out 180s.
    assert not any(argv[:2] == ("pct", "exec") for argv in guest.commands)
    assert response["settling"]["rounds"] == 0
    assert response["settling"]["settled_seconds"] < 1.0


def test_a_round_slower_than_the_span_bound_is_never_decisive() -> None:
    """A round whose own guest commands took too long to answer may be
    describing state that has already moved on -- it can never be decisive,
    whichever way it points."""

    guest = FakeGuest()
    slow_clock = FakeClock()
    original_systemctl = guest._systemctl
    calls = {"n": 0}

    def slow(tail):
        calls["n"] += 1
        if calls["n"] == 2:
            # Simulate a slow second round by advancing the clock past the
            # span bound from inside the guest call itself.
            slow_clock.t += helper.MAX_ROUND_SPAN_SECONDS + 1
        return original_systemctl(tail)

    guest._systemctl = slow
    response = _evaluate_full(
        guest, (("systemd_unit_active", "nginx.service"),), clock=slow_clock
    )
    # Round 2 was slow and non-decisive despite an all-PASS observation;
    # round 3 (fast again) is the first that can actually terminate.
    assert calls["n"] >= 3
    assert response["probes"][0]["outcome"] == "passed"


# ===========================================================================
# 5. Live target revalidation, and whole-request refusals
# ===========================================================================


def test_every_guest_command_revalidates_the_live_target_first() -> None:
    """The dispatcher owns the invariant, so no caller can amortize one
    check across two commands and send a later one to a replacement guest.

    `MIN_DECISIVE_ROUND = 2` already guarantees at least two separate
    `systemctl show` guest commands for an otherwise-passing single-probe
    contract, which is enough distinct commands to prove the invariant now
    that Docker health probes no longer exist to provide a second family."""

    guest = FakeGuest()
    _evaluate(guest, (("systemd_unit_active", "nginx.service"),))
    sequence = [
        "revalidate" if argv[2] == "/cluster/resources" else "guest"
        for argv in guest.commands
        if argv[:2] in (("pvesh", "get"), ("pct", "exec"))
        and (argv[:2] == ("pct", "exec") or argv[2] == "/cluster/resources")
    ]
    assert sequence
    # Every guest command is immediately preceded by its own fresh check.
    for index, entry in enumerate(sequence):
        if entry == "guest":
            assert sequence[index - 1] == "revalidate"


# ===========================================================================
# PR #80 review finding 1: a single stale ``remaining_budget`` reused across
# multiple subprocess invocations could let one logical command path (most
# concretely, live-target revalidation followed by the actual guest command
# inside one `_run_guest_command` call) consume roughly TWICE its intended
# share of the 180s settling deadline. Every timeout is now computed FRESH,
# immediately before the specific subprocess it bounds, by reading the clock
# again -- never a value calculated before an earlier subprocess in the same
# chain consumed real wall-clock time. These tests make subprocess calls
# consume real (virtual) time via `FakeGuest.consume_seconds_by_call_index`,
# which the ordinary always-instant fake clock could never exercise.
# ===========================================================================


def test_revalidation_that_consumes_most_of_the_budget_does_not_hand_the_actual_command_the_old_full_timeout() -> None:
    """Witness A. A single `guest_operational` probe issues exactly one
    `_run_guest_command` call: its own live-target revalidation (call index
    2, after `_local_node` and the pre-flight revalidation), then the actual
    `/bin/true` (call index 3). Before this fix both calls received the
    SAME timeout, computed once before either ran; this proves the second
    call's timeout reflects what the first call actually consumed."""

    guest = FakeGuest()
    clock = FakeClock()
    guest.clock = clock
    guest.consume_seconds_by_call_index = {2: 20.0}

    payload = _request((("guest_operational", None),))
    payload["settling_policy"]["deadline_seconds"] = (
        helper.MIN_SETTLING_DEADLINE_SECONDS
    )
    response = helper.handle_request(
        payload, runner=guest, monotonic=clock.monotonic, sleep=clock.sleep
    )
    assert response["ok"] is True

    revalidation_timeout, command_timeout = guest.call_timeouts[2], guest.call_timeouts[3]
    # The old bug: both calls would receive the identical value computed once
    # by the caller before either subprocess ran.
    assert command_timeout < revalidation_timeout
    # Not merely smaller -- meaningfully smaller, reflecting the ~20s the
    # revalidation call actually consumed.
    assert command_timeout < revalidation_timeout - 15


def test_a_round_that_exhausts_the_budget_stops_the_next_round_from_starting_at_all() -> None:
    """Witness B. Docker health probes no longer exist to provide a second
    FAMILY sharing one round with systemd, so this now proves the same
    invariant across ROUNDS of the one remaining family: round 1's own
    `systemctl show` is made to consume nearly its entire granted timeout,
    leaving no real budget for round 2 -- which must then never even ATTEMPT
    its own revalidation, proving a later round cannot begin once an earlier
    one has exhausted the real budget, not only that its command would get a
    small timeout."""

    guest = FakeGuest()
    clock = FakeClock()
    guest.clock = clock
    # Call index 3 is round 1's own `systemctl show` -- consume its ENTIRE
    # granted timeout (a real bounded command can never consume more than the
    # timeout it was actually given).
    guest.consume_seconds_by_call_index = {3: 1_000_000.0}

    payload = _request((("systemd_unit_active", "nginx.service"),))
    payload["settling_policy"]["deadline_seconds"] = (
        helper.MIN_SETTLING_DEADLINE_SECONDS
    )
    response = helper.handle_request(
        payload, runner=guest, monotonic=clock.monotonic, sleep=clock.sleep
    )
    assert response["ok"] is True

    # Round 2's own revalidation was never launched: exactly the 4
    # pre-existing calls (_local_node, pre-flight revalidate, round 1's own
    # revalidate, round 1's `systemctl show`) happened, and nothing after --
    # whether or not the loop logically counts a further budget-exhausted
    # round with zero real commands of its own.
    assert len(guest.commands) == 4
    # Never decisive: round 1's own span vastly exceeds MAX_ROUND_SPAN_
    # SECONDS, so it could never be decisive even before the budget ran out.
    assert response["evaluation_status"] == "unresolved"
    # The actual wall-clock runtime never ran meaningfully past the intended
    # deadline -- only the bounded transport-return margin's worth, not a
    # second full command's timeout on top of it (the original bug).
    assert clock.t <= helper.MIN_SETTLING_DEADLINE_SECONDS + 5.0


def test_sufficient_budget_still_lets_the_normal_two_round_pass_path_through() -> None:
    """Positive control: with ample remaining budget (the ordinary case),
    fresh-per-command timeout computation changes nothing about the normal
    settling behaviour -- `MIN_DECISIVE_ROUND` still requires exactly two
    rounds, the verdict still comes through, and every granted timeout stays
    at the code-owned command ceiling rather than being starved merely
    because the budget accounting is now computed fresh per call."""

    guest = FakeGuest()
    clock = FakeClock()
    guest.clock = clock
    response = _evaluate_full(guest, (("systemd_unit_active", "nginx.service"),), clock=clock)
    assert response["evaluation_status"] == "decisive"
    assert response["probes"] == [
        {
            "index": 0,
            "kind": "systemd_unit_active",
            "target": "nginx.service",
            "outcome": "passed",
            "reason": "unit_active",
        }
    ]
    assert response["settling"]["rounds"] == 2
    assert all(
        timeout >= helper.COMMAND_TIMEOUT_SECONDS - 1.0 for timeout in guest.call_timeouts
    )


def test_a_guest_that_moved_node_refuses_the_whole_evaluation() -> None:
    guest = FakeGuest()
    guest.current_node = "pve-b"
    clock = FakeClock()
    response = helper.handle_request(
        _request((("systemd_unit_active", "nginx.service"),)),
        runner=guest,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    assert response["ok"] is False
    assert response["error"]["classification"] == "stale_target"


def test_a_guest_that_is_not_running_refuses_the_whole_evaluation() -> None:
    guest = FakeGuest()
    guest.running = False
    clock = FakeClock()
    response = helper.handle_request(
        _request((("systemd_unit_active", "nginx.service"),)),
        runner=guest,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    assert response["ok"] is False
    assert response["error"]["classification"] == "guest_unavailable"


def test_a_guest_replaced_mid_evaluation_makes_that_probe_unknown() -> None:
    """A whole-host problem observed mid-round is never a failure of the
    workload the operator declared."""

    guest = FakeGuest()
    calls = {"n": 0}
    original = guest.__call__

    def moving(argv, timeout, max_output):
        if argv[:3] == ("pvesh", "get", "/cluster/resources"):
            calls["n"] += 1
            if calls["n"] > 2:
                guest.current_node = "pve-b"
        return original(argv, timeout, max_output)

    clock = FakeClock()
    response = helper.handle_request(
        _request((("systemd_unit_active", "nginx.service"),)),
        runner=moving,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    assert response["ok"] is True
    assert any(probe["outcome"] == "unknown" for probe in response["probes"])


# ===========================================================================
# 6. The request boundary
# ===========================================================================


def test_the_helper_accepts_exactly_one_request_shape() -> None:
    for payload in (
        {},
        {"request_version": 2, "operation": "evaluate_health_contract"},
        {**_request((("systemd_unit_active", "a.service"),)), "extra": 1},
    ):
        with pytest.raises(helper.RequestError):
            helper.validate_request(payload)


# ===========================================================================
# Timing policy: backend-owned, but clamped against this file's own hard
# ceilings. PR #80 review finding 2.5.
# ===========================================================================


def test_the_helper_refuses_a_malformed_settling_policy_shape() -> None:
    payload = _request((("systemd_unit_active", "a.service"),))
    payload["settling_policy"] = {"deadline_seconds": 180.0}
    with pytest.raises(helper.RequestError, match="settling_policy"):
        helper.validate_request(payload)


@pytest.mark.parametrize("bad_value", ("180", True, None, [180]))
def test_the_helper_refuses_a_non_numeric_settling_policy_value(bad_value) -> None:
    payload = _request((("systemd_unit_active", "a.service"),))
    payload["settling_policy"] = {
        "deadline_seconds": bad_value,
        "observation_interval_seconds": 5.0,
    }
    with pytest.raises(helper.RequestError):
        helper.validate_request(payload)


@pytest.mark.parametrize("bad_value", (0, -5, -0.001))
def test_the_helper_refuses_a_non_positive_settling_policy_value(bad_value) -> None:
    payload = _request((("systemd_unit_active", "a.service"),))
    payload["settling_policy"] = {
        "deadline_seconds": bad_value,
        "observation_interval_seconds": 5.0,
    }
    with pytest.raises(helper.RequestError):
        helper.validate_request(payload)


def test_the_helper_clamps_an_oversized_requested_deadline() -> None:
    """The backend states its policy; this file never trusts it past its
    OWN hard ceilings -- a compromised or buggy backend cannot request an
    arbitrarily large settling window."""

    payload = _request((("systemd_unit_active", "a.service"),))
    payload["settling_policy"] = {
        "deadline_seconds": 10_000.0,
        "observation_interval_seconds": 5.0,
    }
    request = helper.validate_request(payload)
    assert request["settling_deadline_seconds"] == helper.MAX_SETTLING_DEADLINE_SECONDS


def test_the_helper_clamps_an_undersized_requested_deadline() -> None:
    payload = _request((("systemd_unit_active", "a.service"),))
    payload["settling_policy"] = {
        "deadline_seconds": 0.5,
        "observation_interval_seconds": 5.0,
    }
    request = helper.validate_request(payload)
    assert request["settling_deadline_seconds"] == helper.MIN_SETTLING_DEADLINE_SECONDS


def test_the_helper_clamps_an_out_of_range_observation_interval() -> None:
    payload = _request((("systemd_unit_active", "a.service"),))
    payload["settling_policy"] = {
        "deadline_seconds": 180.0,
        "observation_interval_seconds": 999.0,
    }
    request = helper.validate_request(payload)
    assert (
        request["observation_interval_seconds"]
        == helper.MAX_OBSERVATION_INTERVAL_SECONDS
    )


def test_a_requested_deadline_beyond_the_hard_ceiling_never_exceeds_it() -> None:
    """End-to-end proof: a backend that (mistakenly, or maliciously) asked
    for a much larger deadline than the product default never gets a
    settling window longer than this file's own hard ceiling."""

    guest = FakeGuest()
    guest.units["nginx.service"] = ("loaded", "activating", "")
    clock = FakeClock()
    response = helper.handle_request(
        {
            **_request((("systemd_unit_active", "nginx.service"),)),
            "settling_policy": {
                "deadline_seconds": 10_000.0,
                "observation_interval_seconds": 5.0,
            },
        },
        runner=guest,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    assert response["settling"]["settled_seconds"] <= helper.MAX_SETTLING_DEADLINE_SECONDS


def test_the_helper_accepts_the_product_default_policy_unchanged() -> None:
    """The backend's actual product default (180s/5s) passes through
    unclamped -- clamping never silently shrinks the frozen default."""

    payload = _request((("systemd_unit_active", "a.service"),))
    request = helper.validate_request(payload)
    assert request["settling_deadline_seconds"] == helper.DEFAULT_SETTLING_DEADLINE_SECONDS
    assert (
        request["observation_interval_seconds"]
        == helper.DEFAULT_OBSERVATION_INTERVAL_SECONDS
    )


def test_the_helper_refuses_an_unsupported_probe_kind() -> None:
    payload = _request((("systemd_unit_active", "a.service"),))
    payload["health_contract"]["probes"][0]["kind"] = "run_this_command"
    with pytest.raises(helper.RequestError, match="unsupported probe kind"):
        helper.validate_request(payload)


def test_the_helper_refuses_an_empty_or_oversized_probe_set() -> None:
    payload = _request(())
    payload["health_contract"]["probes"] = []
    with pytest.raises(helper.RequestError):
        helper.validate_request(payload)
    payload["health_contract"]["probes"] = [
        {"index": index, "kind": "systemd_unit_active", "target": f"u{index}.service"}
        for index in range(33)
    ]
    with pytest.raises(helper.RequestError):
        helper.validate_request(payload)


def test_the_helper_refuses_non_canonical_or_duplicated_probes() -> None:
    payload = _request(
        (("systemd_unit_active", "a.service"), ("systemd_unit_active", "a.service"))
    )
    with pytest.raises(helper.RequestError, match="repeat a probe"):
        helper.validate_request(payload)

    payload = _request((("systemd_unit_active", "a.service"),))
    payload["health_contract"]["probes"][0]["index"] = 3
    with pytest.raises(helper.RequestError, match="canonically indexed"):
        helper.validate_request(payload)


def test_the_helper_refuses_remote_command_text(monkeypatch) -> None:
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "systemctl is-active anything")
    import io

    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    captured = io.StringIO()
    monkeypatch.setattr(sys, "stdout", captured)
    assert helper.main() == 2
    assert "remote command text is not accepted" in captured.getvalue()


# ===========================================================================
# 7. The real transport, over a real JSON round trip
# ===========================================================================


def _transport(runner):
    from app.package_update_health_host_control import (
        SshPackageUpdateHealthHostControl,
    )

    return SshPackageUpdateHealthHostControl(
        host="pve.example.internal",
        port=22,
        user="hubinet-health",
        private_key_path=Path("/etc/hubinet-ops/health.key"),
        known_hosts_path=Path("/etc/hubinet-ops/health.known_hosts"),
        timeout_seconds=60,
        max_result_bytes=64 * 1024,
        runner=runner,
    )


def _round_trip_runner(guest, clock: FakeClock | None = None):
    """Run the REAL helper against the fake guest, over real JSON bytes."""

    from app.package_scan_host_control import BoundedProcessResult

    clock = clock or FakeClock()

    def runner(argv, stdin, timeout, max_bytes):
        assert argv[0] == "ssh"
        # Pinned trust, no password, no forwarding, no interactive shell.
        assert "BatchMode=yes" in argv
        assert "StrictHostKeyChecking=yes" in argv
        assert "PasswordAuthentication=no" in argv
        assert "ForwardAgent=no" in argv
        payload = json.loads(stdin.decode("utf-8"))
        response = helper.handle_request(
            payload, runner=guest, monotonic=clock.monotonic, sleep=clock.sleep
        )
        return BoundedProcessResult(
            returncode=0 if response.get("ok") else 1,
            stdout=json.dumps(response).encode("utf-8"),
            stderr=b"",
            timed_out=False,
            output_exceeded=False,
        )

    return runner


def test_a_real_round_trip_produces_typed_probe_results(tmp_path: Path) -> None:
    from tests.test_package_update_health import _mutated_job

    _, _, authority, _, _, _, job = _mutated_job(tmp_path)
    request = authority.package_update_health_request(job.job_id)
    guest = FakeGuest()
    guest.vmid = request.vmid
    guest.node = guest.current_node = request.expected_node
    guest.units["nginx.service"] = ("loaded", "active", "")
    guest.units["postgresql.service"] = ("loaded", "active", "")

    result = _transport(_round_trip_runner(guest)).evaluate_health_contract(request)

    assert result.contract_revision == job.health_contract_revision
    assert result.contract_fingerprint == job.health_contract_fingerprint
    assert [(probe.probe_index, probe.outcome.value) for probe in result.probes] == [
        (0, "passed"),
        (1, "passed"),
    ]
    assert result.settling_rounds == 2
    assert result.settling_seconds is not None
    assert result.last_round_span_ms is not None


def test_the_transport_refuses_an_answer_about_another_job(tmp_path: Path) -> None:
    from app.package_update_health import PackageUpdateHealthError
    from app.package_scan_host_control import BoundedProcessResult
    from tests.test_package_update_health import _mutated_job

    _, _, authority, _, _, _, job = _mutated_job(tmp_path)
    request = authority.package_update_health_request(job.job_id)

    def runner(argv, stdin, timeout, max_bytes):
        return BoundedProcessResult(
            returncode=0,
            stdout=json.dumps(
                {
                    "response_version": 1,
                    "ok": True,
                    "job_id": str(uuid.uuid4()),
                    "health_contract": {
                        "revision": request.health_contract_revision,
                        "fingerprint": request.health_contract_fingerprint,
                    },
                    "probes": [],
                }
            ).encode(),
            stderr=b"",
            timed_out=False,
            output_exceeded=False,
        )

    with pytest.raises(PackageUpdateHealthError, match="different package update job"):
        _transport(runner).evaluate_health_contract(request)


@pytest.mark.parametrize(
    "payload",
    (
        {"response_version": 2},
        {"response_version": 1, "ok": False, "error": {"classification": "boom"}},
        {"response_version": 1, "ok": True, "health_contract": {}, "probes": []},
        {
            "response_version": 1,
            "ok": True,
            "health_contract": {"revision": 4, "fingerprint": "a" * 64},
            "probes": [
                {
                    "index": 0,
                    "kind": "systemd_unit_active",
                    "target": "nginx.service",
                    "outcome": "passed",
                    "reason": "Active: active (running)",
                }
            ],
        },
        {
            "response_version": 1,
            "ok": True,
            "health_contract": {"revision": 4, "fingerprint": "a" * 64},
            "probes": [
                {
                    "index": 0,
                    "kind": "run_this_command",
                    "target": "x",
                    "outcome": "passed",
                    "reason": "unit_active",
                }
            ],
        },
    ),
)
def test_a_malformed_host_response_never_becomes_a_result(
    tmp_path: Path, payload
) -> None:
    from app.package_update_health import PackageUpdateHealthError
    from app.package_scan_host_control import BoundedProcessResult
    from tests.test_package_update_health import _mutated_job

    _, _, authority, _, _, _, job = _mutated_job(tmp_path)
    request = authority.package_update_health_request(job.job_id)
    body = dict(payload)
    body.setdefault("job_id", request.job_id)

    def runner(argv, stdin, timeout, max_bytes):
        return BoundedProcessResult(
            returncode=0,
            stdout=json.dumps(body).encode(),
            stderr=b"",
            timed_out=False,
            output_exceeded=False,
        )

    with pytest.raises(PackageUpdateHealthError):
        _transport(runner).evaluate_health_contract(request)


@pytest.mark.parametrize(
    "result_kwargs",
    (
        {"timed_out": True},
        {"output_exceeded": True},
        {"returncode": 255, "stdout": b""},
        {"stdout": b"not json at all"},
    ),
)
def test_a_lost_or_unreadable_answer_raises_rather_than_returning(
    tmp_path: Path, result_kwargs
) -> None:
    from app.package_update_health import PackageUpdateHealthError
    from app.package_scan_host_control import BoundedProcessResult
    from tests.test_package_update_health import _mutated_job

    _, _, authority, _, _, _, job = _mutated_job(tmp_path)
    request = authority.package_update_health_request(job.job_id)
    defaults = {
        "returncode": 0,
        "stdout": b"{}",
        "stderr": b"",
        "timed_out": False,
        "output_exceeded": False,
    }
    defaults.update(result_kwargs)

    def runner(argv, stdin, timeout, max_bytes):
        return BoundedProcessResult(**defaults)

    with pytest.raises(PackageUpdateHealthError):
        _transport(runner).evaluate_health_contract(request)


# ===========================================================================
# 8. Cross-node routing: the one place a command line exists
# ===========================================================================


class RemoteGuest(FakeGuest):
    """The same guest, reachable only on another cluster member."""

    def __init__(self) -> None:
        super().__init__()
        self.node = "pve-b"
        self.current_node = "pve-b"
        self.remote_command_lines: list[str] = []

    def __call__(self, argv, timeout, max_output):
        if argv[0] == "ssh":
            assert argv[-2] == "root@pve-b", argv
            self.remote_command_lines.append(argv[-1])
            import shlex

            return super().__call__(tuple(shlex.split(argv[-1])), timeout, max_output)
        if argv[:2] == ("pvesh", "get") and argv[2] == "/cluster/status":
            # This helper runs on pve-a; the guest lives on pve-b.
            return self._ok(
                json.dumps([{"type": "node", "name": "pve-a", "local": 1}]).encode()
            )
        return super().__call__(argv, timeout, max_output)


def test_a_remote_guest_is_probed_through_a_command_line_needing_no_quoting() -> None:
    """Shell quoting is not the mechanism, and this proves it.

    Routing to another cluster member is the one place an argv list becomes
    command text, because that is what ssh hands the remote login shell. The
    target's charset already contains nothing a shell reads, so `shlex.join`
    must render every element as a bare word -- if it ever had to add a quote,
    the target is not what this file thinks it is.
    """

    guest = RemoteGuest()
    guest.units["worker.service"] = ("loaded", "active", "")
    assert _evaluate(
        guest,
        (
            ("systemd_unit_active", "nginx.service"),
            ("systemd_unit_active", "worker.service"),
        ),
        node="pve-b",
    ) == [("passed", "unit_active"), ("passed", "unit_active")]

    assert guest.remote_command_lines
    # The caller-derived elements appear as bare words, batched into one
    # `systemctl show` call ending with both targets in the request's own
    # canonical order.
    assert any(
        line.endswith(" -- nginx.service worker.service")
        for line in guest.remote_command_lines
    )


def test_an_element_that_would_need_quoting_is_refused_rather_than_quoted() -> None:
    """The assertion is load-bearing, not decorative.

    The kind-specific validation makes such a target unreachable, so this
    reaches past it to prove the routing boundary refuses on its own rather
    than relying on a single upstream check.
    """

    guest = RemoteGuest()
    clock = FakeClock()
    with pytest.raises(helper.ProbeUnknown, match="probe_target_not_exact"):
        helper._run_guest_command(
            guest,
            VMID,
            "pve-b",
            "pve-a",
            ("env", "LC_ALL=C", "systemctl", "show", "--", "a b.service"),
            data_arguments=("a b.service",),
            absolute_deadline=clock.t + 60.0,
            monotonic=clock.monotonic,
        )
    assert guest.remote_command_lines == []


def test_the_helper_can_only_report_reasons_the_backend_accepts() -> None:
    """A closed taxonomy is only closed if both ends agree on it.

    Every reason literal the helper's classifiers can produce must be one the
    transport parses and the authority stores. A helper token the backend
    rejects would silently turn a truthful probe result into a rejected
    response -- and a backend token the helper could never send would be dead
    vocabulary.
    """

    from app.inventory import HEALTH_PROBE_REASONS
    from app.package_update_health import HOST_PROBE_REASONS

    assert HOST_PROBE_REASONS <= HEALTH_PROBE_REASONS

    produced: set[str] = set()
    produced.update(helper._SYSTEMD_TRANSIENT_REASONS.values())
    produced.update(
        {
            "unit_active",
            "unit_not_active",
            "unit_job_pending",
            "probe_target_not_exact",
            "probe_target_ambiguous",
            "malformed_output",
            "command_failed",
            "command_timed_out",
            "guest_unavailable",
            "guest_operational_confirmed",
            "settling_budget_exhausted",
        }
    )
    assert produced <= HOST_PROBE_REASONS, produced - HOST_PROBE_REASONS


def test_backend_eligibility_and_standalone_helper_grammars_are_identical() -> None:
    """Issuance must never accept a target the standalone helper refuses."""

    from app.inventory.health_execution import (
        SYSTEMD_UNIT_PATTERN,
        SYSTEMD_UNIT_SUFFIXES,
    )

    assert helper.SYSTEMD_UNIT_RE.pattern == SYSTEMD_UNIT_PATTERN
    assert helper.SYSTEMD_UNIT_SUFFIXES == SYSTEMD_UNIT_SUFFIXES


# ===========================================================================
# PR #80 FINAL REVIEW MINOR-1: ONE absolute deadline, prologue included.
#
# `_local_node` and the first `revalidate_live_target` used to run at the
# unconditional 60s per-command allowance BEFORE any deadline existed, so a
# bounded operation's real wall clock was "prologue + deadline" rather than
# "deadline" -- up to ~302s against a 300s outer SSH transport timeout.
# These inspect the ACTUAL timeouts granted to subprocesses and the actual
# commands launched, never only the final status.
# ===========================================================================


def test_the_health_prologue_spends_the_same_budget_as_the_settling_rounds() -> None:
    """Witness: the local-node read consumes real time, then the pre-flight
    revalidation consumes more, and the FIRST guest command of round 1 is
    granted only what is genuinely left -- not a fresh full allowance."""

    guest = FakeGuest()
    clock = FakeClock()
    guest.clock = clock
    # Call 0 is `_local_node`; call 1 is the pre-flight revalidation.
    guest.consume_seconds_by_call_index = {0: 12.0, 1: 9.0}

    payload = _request((("guest_operational", None),))
    payload["settling_policy"]["deadline_seconds"] = (
        helper.MIN_SETTLING_DEADLINE_SECONDS
    )
    response = helper.handle_request(
        payload, runner=guest, monotonic=clock.monotonic, sleep=clock.sleep
    )
    assert response["ok"] is True

    local_node_timeout = guest.call_timeouts[0]
    revalidation_timeout = guest.call_timeouts[1]
    first_round_timeout = guest.call_timeouts[2]
    # Before the fix `_local_node` got COMMAND_TIMEOUT_SECONDS unconditionally.
    assert local_node_timeout < helper.COMMAND_TIMEOUT_SECONDS
    assert local_node_timeout <= helper.MIN_SETTLING_DEADLINE_SECONDS
    # Each later prologue/round command sees strictly less budget than the
    # one before it, because they all draw down ONE deadline.
    assert revalidation_timeout < local_node_timeout - 11
    assert first_round_timeout < revalidation_timeout - 8
    # And the whole operation stayed inside the requested deadline.
    assert clock.t <= helper.MIN_SETTLING_DEADLINE_SECONDS + 0.1


def test_a_health_prologue_that_exhausts_the_budget_launches_no_guest_command() -> None:
    """Fail-closed, and never a verdict: if the prologue consumes the whole
    deadline, the settling rounds never start at all."""

    guest = FakeGuest()
    clock = FakeClock()
    guest.clock = clock
    guest.consume_seconds_by_call_index = {0: 1_000_000.0}

    payload = _request((("systemd_unit_active", "nginx.service"),))
    payload["settling_policy"]["deadline_seconds"] = (
        helper.MIN_SETTLING_DEADLINE_SECONDS
    )
    response = helper.handle_request(
        payload, runner=guest, monotonic=clock.monotonic, sleep=clock.sleep
    )
    # A whole-request refusal, never a probe verdict and never "healthy".
    assert response["ok"] is False
    assert response["error"]["classification"] == "execution_failed"
    # Exactly one command ran -- the local-node read that ate the budget.
    assert len(guest.commands) == 1
    assert not any(argv[:2] == ("pct", "exec") for argv in guest.commands)


# ===========================================================================
# PR #80 FINAL REVIEW MINOR-2: `guest_operational` never reaches a target
# validator, and no `"None"` string is ever manufactured for it.
# ===========================================================================


def test_guest_operational_never_reaches_a_target_validator(monkeypatch) -> None:
    """It carries no target, and it never even reaches the advanced settling
    loop's `_structural_probe_outcome` at all -- the baseline is a
    structurally SEPARATE one-shot code path
    (`_evaluate_guest_operational_once`), called directly by
    `handle_request`, never through the systemd settling machinery. So
    `_require_exact_systemd_unit` must never be called while evaluating a
    `guest_operational` contract -- and certainly not via `str(None)`, which
    happens to look like a legal systemd unit name once suffixed."""

    called: list[object] = []
    real_systemd = helper._require_exact_systemd_unit

    def spy_systemd(target):
        called.append(target)
        return real_systemd(target)

    monkeypatch.setattr(helper, "_require_exact_systemd_unit", spy_systemd)

    guest = FakeGuest()
    _evaluate(guest, (("guest_operational", None),))
    assert called == []

    # Positive control: the advanced kind still validates its target.
    _evaluate(guest, (("systemd_unit_active", "nginx.service"),))
    assert called == ["nginx.service"]
    assert "None" not in called


def test_an_unknown_probe_kind_is_never_validated_as_something_it_is_not() -> None:
    """The advanced settling loop's structural check is exact kind branching,
    never an ``else:`` catch-all that would make an unrecognised kind look
    like a valid one."""

    assert helper._structural_probe_outcome("podman_container_running", "web") == (
        "unknown",
        "malformed_output",
    )


def test_a_kinded_probe_with_a_non_string_target_is_unknown_not_stringified() -> None:
    assert helper._structural_probe_outcome("systemd_unit_active", None) == (
        "unknown",
        "probe_target_not_exact",
    )
    assert helper._structural_probe_outcome("systemd_unit_active", 12) == (
        "unknown",
        "probe_target_not_exact",
    )


def test_guest_operational_carries_none_end_to_end_with_no_fake_string() -> None:
    """The whole round trip: request in, guest command run, response out --
    `None` stays `None`, and the literal string "None" appears nowhere."""

    guest = FakeGuest()
    response = _evaluate_full(guest, (("guest_operational", None),))
    assert response["probes"][0]["target"] is None
    assert response["probes"][0]["outcome"] == "passed"
    assert response["probes"][0]["reason"] == "guest_operational_confirmed"
    assert "None" not in json.dumps(response)
    # The fixed liveness command ran; no target became an argv element.
    guest_commands = [argv for argv in guest.commands if argv[:2] == ("pct", "exec")]
    assert any(argv[-1:] == ("/bin/true",) for argv in guest_commands)
    assert not any("None" in element for argv in guest.commands for element in argv)
