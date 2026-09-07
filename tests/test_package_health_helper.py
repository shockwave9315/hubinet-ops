"""The dark health helper's fixed argv, bounded settling, and every way it
must not false-PASS.

The helper is the one place in this repository where an operator-supplied
string becomes part of a command line, so this file is deliberately adversarial
about it. It drives the REAL helper module against a fake guest that answers
exactly the fixed argv shapes the helper issues -- an unrecognised command is a
test failure, not a silent empty result -- and it asserts the argv itself, not
only the verdict.

The CLI behaviours these tests encode were verified against the real tools
(systemd 257, Docker 26.1.5) rather than assumed; `ARCHITECTURE.md`,
"Job-bound healthcheck execution", records what was observed. In particular:

- `systemctl is-active` expands globs and succeeds if ANY match is active,
  and `--` does not stop it -- which is why this helper does not use it;
- a systemd glob can match exactly ONE unit, so "one property block" alone is
  not enough and the target charset must exclude `*`, `?`, `[`;
- `ssh.service` and `sshd.service` are aliases and report the SAME `Id`, so
  batched systemd results are mapped BY POSITION, never by `Id`;
- `docker inspect` resolves by ID prefix as well as by name, and a missing
  name among several batched targets does not shift or suppress the others,
  so batched Docker results are mapped BY NAME, never by position;
- `docker inspect` cannot distinguish "no such container" from "daemon
  unavailable" by exit code.

Every scenario below runs through a fake, instantly-advancing clock: bounded
settling requires at least `MIN_DECISIVE_ROUND` (2) rounds before any verdict,
so `_evaluate` always injects a deterministic clock rather than sleeping for
real. Nothing here runs a real `pvesh`, `pct`, `ssh`, `systemctl`, or `docker`.
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

    Its systemd and Docker behaviour models what the real tools were OBSERVED
    to do, including the parts that make a naive probe unsafe: `systemctl
    show` emits one blank-line-separated property block per matched unit and
    expands `*`, `?`, `[`; `docker inspect` resolves by ID prefix and reports
    the container's own name with a leading slash.
    """

    def __init__(self) -> None:
        self.vmid = VMID
        self.node = NODE
        self.present = True
        self.running = True
        self.resource_type = "lxc"
        self.current_node = NODE
        #: unit id -> (LoadState, ActiveState, Job)
        self.units: dict[str, tuple[str, str, str]] = {
            "nginx.service": ("loaded", "active", ""),
            "worker.service": ("loaded", "failed", ""),
        }
        #: Alias -> canonical unit id, exactly as systemd resolves one.
        self.unit_aliases: dict[str, str] = {}
        #: container name -> (Status, Restarting, Health), "<none>" health
        #: for a container that declares no HEALTHCHECK.
        self.containers: dict[str, tuple[str, str, str]] = {
            "web": ("running", "false", "healthy"),
        }
        self.docker_daemon_up = True
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
        if command == "docker":
            return self._docker(tail)
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

    # -- docker --------------------------------------------------------

    def _docker(self, tail):
        if tail[3] == "ps":
            assert tuple(tail[3:]) == (
                "ps",
                "--all",
                "--no-trunc",
                "--format",
                helper.DOCKER_NAME_LIST_FORMAT,
            ), tail
            if not self.docker_daemon_up:
                return helper.CommandResult(
                    1, b"", b"Cannot connect to the Docker daemon"
                )
            return self._ok(
                b"".join(
                    json.dumps(name).encode() + b"\n" for name in self.containers
                )
            )
        assert tail[3] == "inspect", tail
        assert tuple(tail[4:6]) == ("--type", "container"), tail
        assert tail[6] == helper.DOCKER_INSPECT_FORMAT_FLAG, tail
        # The template is a CONSTANT owned by the helper, byte for byte.
        assert tail[7] == helper.DOCKER_INSPECT_FORMAT, tail
        assert tail[8] == "--", tail
        if self.timeout_on == "docker":
            return helper.CommandResult(0, b"", b"", timed_out=True)
        if not self.docker_daemon_up:
            return helper.CommandResult(
                1, b"", b"Cannot connect to the Docker daemon"
            )
        requested = tail[9:]
        lines = []
        errors = []
        for name in requested:
            resolved = self._resolve_container(name)
            if resolved is None:
                errors.append(name)
                continue
            resolved_name, (status, restarting, health) = resolved
            lines.append(f"/{resolved_name}\t{status}\t{restarting}\t{health}")
        stdout = ("\n".join(lines) + "\n").encode() if lines else b""
        returncode = 1 if errors else 0
        return helper.CommandResult(returncode, stdout, b"boom" if errors else b"")

    def _resolve_container(self, requested: str):
        if requested in self.containers:
            return requested, self.containers[requested]
        # Docker also resolves a container by ID PREFIX, and then reports the
        # container's real name -- which is what makes an exact-name check
        # load-bearing rather than decorative.
        for name in self.containers:
            if _fake_container_id(name).startswith(requested) and len(requested) >= 4:
                return name, self.containers[name]
        return None

    # -- helpers -------------------------------------------------------

    @staticmethod
    def _ok(stdout: bytes):
        return helper.CommandResult(0, stdout, b"")


def _fake_container_id(name: str) -> str:
    import hashlib

    return hashlib.sha256(name.encode()).hexdigest()


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
# 2. docker_container_running
# ===========================================================================


def test_a_running_container_passes_with_the_exact_fixed_argv() -> None:
    guest = FakeGuest()
    assert _evaluate(guest, (("docker_container_running", "web"),)) == [
        ("passed", "container_running")
    ]
    inspect = next(
        argv
        for argv in guest.commands
        if argv[:2] == ("pct", "exec") and "inspect" in argv
    )
    assert inspect == (
        "pct",
        "exec",
        str(VMID),
        "--",
        "env",
        "LC_ALL=C",
        "docker",
        "inspect",
        "--type",
        "container",
        "--format",
        helper.DOCKER_INSPECT_FORMAT,
        "--",
        "web",
    )
    # No pipeline, no grep, no `docker ps` parsing for the verdict itself.
    assert all("|" not in element for argv in guest.commands for element in argv)


@pytest.mark.parametrize(
    ("status", "reason"),
    (
        ("exited", "container_not_running"),
        ("dead", "container_not_running"),
        ("paused", "container_not_running"),
    ),
)
def test_a_settled_non_running_container_is_a_definitive_failure(
    status: str, reason: str
) -> None:
    guest = FakeGuest()
    guest.containers["web"] = (status, "false", "<none>")
    assert _evaluate(guest, (("docker_container_running", "web"),)) == [
        ("failed", reason)
    ]


@pytest.mark.parametrize(
    ("status", "restarting", "reason"),
    (
        ("created", "false", "container_not_started_yet"),
        ("restarting", "true", "container_restarting"),
        ("removing", "false", "container_removing"),
    ),
)
def test_docker_own_transient_lifecycle_states_are_unknown_not_failed(
    status: str, restarting: str, reason: str
) -> None:
    """Docker's own transient lifecycle, entered automatically and expected
    to resolve on its own -- never a workload verdict, for either Docker
    probe kind."""

    guest = FakeGuest()
    guest.containers["web"] = (status, restarting, "<none>")
    assert _evaluate(guest, (("docker_container_running", "web"),)) == [
        ("unknown", reason)
    ]
    assert _evaluate(guest, (("docker_container_healthy", "web"),)) == [
        ("unknown", reason)
    ]


def test_an_absent_container_is_a_failure_only_because_the_daemon_answered() -> None:
    guest = FakeGuest()
    assert _evaluate(guest, (("docker_container_running", "gone"),)) == [
        ("failed", "container_absent")
    ]
    # The one daemon-oracle-and-absence-proof call ran; no inspect for a name
    # it never enumerated.
    ps_calls = [
        argv
        for argv in guest.commands
        if argv[:2] == ("pct", "exec") and "ps" in argv
    ]
    assert ps_calls
    assert not any(
        argv[:2] == ("pct", "exec") and "inspect" in argv for argv in guest.commands
    )


def test_a_docker_daemon_that_is_down_is_unknown_never_absent() -> None:
    guest = FakeGuest()
    guest.docker_daemon_up = False
    assert _evaluate(guest, (("docker_container_running", "web"),)) == [
        ("unknown", "docker_daemon_unavailable")
    ]


def test_an_id_prefix_can_never_resolve_to_a_pass() -> None:
    """`docker inspect` resolves by ID prefix, but the batched daemon oracle
    (`docker ps` `.Names`) is consulted FIRST and lists real NAMES only, never
    IDs -- so an ID-prefix-looking target is proven absent from the exact
    name universe before `docker inspect` ever gets a chance to resolve it by
    prefix. The ID-prefix confusion the old per-probe design had to defend
    against inside `docker inspect` is structurally unreachable here."""

    guest = FakeGuest()
    prefix = _fake_container_id("web")[:12]
    assert _evaluate(guest, (("docker_container_running", prefix),)) == [
        ("failed", "container_absent")
    ]
    assert not any(
        argv[:2] == ("pct", "exec") and "inspect" in argv for argv in guest.commands
    )


@pytest.mark.parametrize("target", ("--help", "-f", "/web", "web name"))
def test_an_option_or_path_like_container_target_is_refused(target: str) -> None:
    guest = FakeGuest()
    assert _evaluate(guest, (("docker_container_running", target),)) == [
        ("unknown", "probe_target_not_exact")
    ]


def test_malformed_inspect_output_is_unknown() -> None:
    guest = FakeGuest()

    original = guest._docker

    def broken(tail):
        if tail[3] == "inspect":
            return helper.CommandResult(0, b"/web\tmaybe\tfalse\thealthy\n", b"")
        return original(tail)

    guest._docker = broken
    assert _evaluate(guest, (("docker_container_running", "web"),)) == [
        ("unknown", "malformed_output")
    ]


def test_an_inspect_timeout_is_unknown() -> None:
    guest = FakeGuest()
    guest.timeout_on = "docker"
    assert _evaluate(guest, (("docker_container_running", "web"),)) == [
        ("unknown", "command_timed_out")
    ]


def test_a_generic_inspect_failure_is_not_absence_while_name_still_exists() -> None:
    """The daemon oracle already proved the name exists this round; a
    separate inspect glitch afterwards is a race, never absence."""

    guest = FakeGuest()
    original = guest._docker

    def failed(tail):
        if tail[3] == "inspect":
            return helper.CommandResult(1, b"", b"generic inspect failure")
        return original(tail)

    guest._docker = failed
    assert _evaluate(guest, (("docker_container_running", "web"),)) == [
        ("unknown", "command_failed")
    ]


def test_an_unusable_daemon_oracle_is_unknown_for_every_docker_probe() -> None:
    guest = FakeGuest()
    original = guest._docker

    def unusable_listing(tail):
        if tail[3] == "ps":
            return helper.CommandResult(0, b"not-json\n", b"")
        return original(tail)

    guest._docker = unusable_listing
    assert _evaluate(guest, (("docker_container_running", "web"),)) == [
        ("unknown", "malformed_output")
    ]


# ===========================================================================
# 2.5. guest_operational (v20): a FALLBACK, not application health.
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


def test_guest_operational_alongside_a_docker_probe_in_the_same_round() -> None:
    """A mixed contract is legal, and each family stays independently
    batched: one guest_operational command, one Docker round, same round."""

    guest = FakeGuest()
    assert _evaluate(
        guest,
        (
            ("guest_operational", None),
            ("docker_container_running", "web"),
        ),
    ) == [
        ("passed", "guest_operational_confirmed"),
        ("passed", "container_running"),
    ]


# ===========================================================================
# 3. docker_container_healthy
# ===========================================================================


def test_a_healthy_container_passes() -> None:
    guest = FakeGuest()
    assert _evaluate(guest, (("docker_container_healthy", "web"),)) == [
        ("passed", "container_healthy")
    ]


@pytest.mark.parametrize(
    ("state", "reason"),
    (
        (("running", "false", "unhealthy"), "container_unhealthy"),
        (("running", "false", "<none>"), "container_has_no_healthcheck"),
        (("exited", "false", "healthy"), "container_not_running"),
    ),
)
def test_docker_health_is_never_downgraded_to_merely_running(
    state, reason: str
) -> None:
    """The operator asked for Docker HEALTHCHECK health specifically, so none
    of these may be quietly accepted as "well, it is running"."""

    guest = FakeGuest()
    guest.containers["web"] = state
    assert _evaluate(guest, (("docker_container_healthy", "web"),)) == [
        ("failed", reason)
    ]


def test_docker_health_starting_is_unknown_not_failed() -> None:
    """Live Human1 evidence: a package update that restarts Docker/containerd
    puts every container's health state machine through "starting" again on
    a perfectly healthy workload. That is Docker's own transient state, not
    a workload verdict, so it must never become a durable FAIL -- unlike
    `unhealthy`, `<none>`, and not-running above, which stay definitive
    failures because the operator specifically demanded Docker health."""

    guest = FakeGuest()
    guest.containers["web"] = ("running", "false", "starting")
    assert _evaluate(guest, (("docker_container_healthy", "web"),)) == [
        ("unknown", "container_health_starting")
    ]


def test_a_container_running_probe_still_passes_for_an_unhealthy_container() -> None:
    """The two Docker kinds are genuinely different questions."""

    guest = FakeGuest()
    guest.containers["web"] = ("running", "false", "unhealthy")
    assert _evaluate(guest, (("docker_container_running", "web"),)) == [
        ("passed", "container_running")
    ]


def test_an_unknown_health_status_is_unknown_not_a_guess() -> None:
    guest = FakeGuest()
    guest.containers["web"] = ("running", "false", "mysterious")
    assert _evaluate(guest, (("docker_container_healthy", "web"),)) == [
        ("unknown", "malformed_output")
    ]


def test_an_unknown_status_value_is_unknown_not_a_guess() -> None:
    guest = FakeGuest()
    guest.containers["web"] = ("quantum", "false", "<none>")
    assert _evaluate(guest, (("docker_container_running", "web"),)) == [
        ("unknown", "malformed_output")
    ]


# ===========================================================================
# 4. Bounded settling: full-round temporal coherence and decisive rounds
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
    disproves it, and only the LAST complete round's evidence may decide."""

    guest = FakeGuest()
    guest.units["nginx.service"] = ("loaded", "active", "")  # A: PASS round 1
    guest.containers["web"] = ("running", "false", "starting")  # B: TRANSIENT

    calls = {"n": 0}
    original_systemctl = guest._systemctl

    def flipping(tail):
        calls["n"] += 1
        if calls["n"] >= 2:
            # Round 2 onward: A now fails definitively.
            guest.units["nginx.service"] = ("loaded", "failed", "")
        return original_systemctl(tail)

    guest._systemctl = flipping

    original_docker = guest._docker

    def settling(tail):
        if tail[3] == "inspect":
            guest.containers["web"] = ("running", "false", "healthy")  # B: PASS
        return original_docker(tail)

    guest._docker = settling

    response = _evaluate_full(
        guest,
        (
            ("systemd_unit_active", "nginx.service"),
            ("docker_container_healthy", "web"),
        ),
    )
    outcomes = {probe["index"]: probe["outcome"] for probe in response["probes"]}
    assert outcomes[0] == "failed"
    assert response["probes"][0]["reason"] == "unit_not_active"
    # The whole contract is FAILED, never PASSED, because of it.
    assert any(probe["outcome"] == "failed" for probe in response["probes"])


def test_transient_settles_to_pass_within_the_bounded_window() -> None:
    guest = FakeGuest()
    guest.containers["web"] = ("running", "false", "starting")
    calls = {"n": 0}
    original = guest._docker

    def settling(tail):
        calls["n"] += 1
        if tail[3] == "inspect" and calls["n"] >= 3:
            guest.containers["web"] = ("running", "false", "healthy")
        return original(tail)

    guest._docker = settling
    assert _evaluate(guest, (("docker_container_healthy", "web"),)) == [
        ("passed", "container_healthy")
    ]


def test_transient_settles_to_a_definitive_failure() -> None:
    guest = FakeGuest()
    guest.containers["web"] = ("running", "false", "starting")
    calls = {"n": 0}
    original = guest._docker

    def settling(tail):
        calls["n"] += 1
        if tail[3] == "inspect" and calls["n"] >= 3:
            guest.containers["web"] = ("running", "false", "unhealthy")
        return original(tail)

    guest._docker = settling
    assert _evaluate(guest, (("docker_container_healthy", "web"),)) == [
        ("failed", "container_unhealthy")
    ]


def test_persistent_transient_exhausts_the_deadline_as_unknown() -> None:
    """A workload that never settles either way reports UNKNOWN once the
    bounded structural round cap is reached, carrying only the LAST complete
    round's evidence, never a merged or partial one."""

    guest = FakeGuest()
    guest.containers["web"] = ("running", "false", "starting")
    response = _evaluate_full(guest, (("docker_container_healthy", "web"),))
    assert response["probes"] == [
        {
            "index": 0,
            "kind": "docker_container_healthy",
            "target": "web",
            "outcome": "unknown",
            "reason": "container_health_starting",
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
    assert (
        response["settling"]["settled_seconds"]
        >= helper.DEFAULT_SETTLING_DEADLINE_SECONDS
    )
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
    check across two commands and send a later one to a replacement guest."""

    guest = FakeGuest()
    _evaluate(
        guest,
        (
            ("systemd_unit_active", "nginx.service"),
            ("docker_container_running", "web"),
        ),
    )
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


def test_an_earlier_family_that_exhausts_the_budget_stops_a_later_family_from_starting_at_all() -> None:
    """Witness B. A systemd probe (family 1) and a `guest_operational` probe
    (family 2) share one round. The systemd family's own guest command is
    made to consume nearly the entire settling deadline; the guest family
    must then never even ATTEMPT its own revalidation -- proving a later
    family cannot begin once an earlier one has exhausted the real budget,
    not only that its command gets a small timeout."""

    guest = FakeGuest()
    clock = FakeClock()
    guest.clock = clock
    # Call index 3 is the systemd family's own `systemctl show` -- consume
    # its ENTIRE granted timeout (a real bounded command can never consume
    # more than the timeout it was actually given).
    guest.consume_seconds_by_call_index = {3: 1_000_000.0}

    payload = _request(
        (
            ("systemd_unit_active", "nginx.service"),
            ("guest_operational", None),
        )
    )
    payload["settling_policy"]["deadline_seconds"] = (
        helper.MIN_SETTLING_DEADLINE_SECONDS
    )
    response = helper.handle_request(
        payload, runner=guest, monotonic=clock.monotonic, sleep=clock.sleep
    )
    assert response["ok"] is True

    # The guest_operational family's own revalidation (and therefore its
    # `/bin/true`) was never launched: exactly the 4 pre-existing calls
    # (_local_node, pre-flight revalidate, this round's systemd revalidate,
    # this round's `systemctl show`) happened, and nothing after.
    assert len(guest.commands) == 4
    assert not any(argv[-1:] == ("/bin/true",) for argv in guest.commands)

    outcomes = {probe["index"]: probe for probe in response["probes"]}
    assert outcomes[1]["outcome"] == "unknown"
    assert outcomes[1]["reason"] == "settling_budget_exhausted"
    # Controlled unresolved result, never a durable verdict.
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
        _request(
            (
                ("systemd_unit_active", "nginx.service"),
                ("docker_container_running", "web"),
            )
        ),
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
    guest.containers["web"] = ("running", "false", "starting")
    clock = FakeClock()
    response = helper.handle_request(
        {
            **_request((("docker_container_healthy", "web"),)),
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
    guest.containers["web"] = ("running", "false", "healthy")

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


def test_a_real_discovery_round_trip_produces_a_typed_result(tmp_path: Path) -> None:
    """Real authority -> real SSH transport JSON encoding -> the real
    deployed helper's discovery engine -> a typed, bounded result. Proves
    the SECOND operation is genuinely reachable through the same boundary
    the first one already is."""

    from tests.test_package_update_health import _mutated_job

    _, _, authority, resource, _, _, _ = _mutated_job(tmp_path)
    request = authority.resource_health_discovery_request(resource.resource_id)
    guest = DiscoveryGuest()
    guest.vmid = request.vmid
    guest.current_node = request.expected_node
    guest.docker_info = {"web": ("running", "healthcheck")}

    result = _transport(_round_trip_runner(guest)).discover_health_candidates(request)

    assert result.status.value == "ok"
    assert result.recommendation_basis.value == "docker_healthcheck"
    assert len(result.candidates) == 1
    assert result.candidates[0].target == "web"
    assert result.candidates[0].kind.value == "docker_container_healthy"
    assert result.candidates[0].recommended is True


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
    assert _evaluate(
        guest,
        (
            ("systemd_unit_active", "nginx.service"),
            ("docker_container_running", "web"),
        ),
        node="pve-b",
    ) == [("passed", "unit_active"), ("passed", "container_running")]

    assert guest.remote_command_lines
    # The caller-derived elements appear as bare words. The Docker format
    # template is a constant this file owns and is quoted normally.
    assert any(line.endswith(" -- nginx.service") for line in guest.remote_command_lines)
    assert any(line.endswith(" -- web") for line in guest.remote_command_lines)


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
    produced.update(helper._DOCKER_TRANSIENT_REASONS.values())
    produced.update(helper._SYSTEMD_TRANSIENT_REASONS.values())
    produced.update(
        {
            "unit_active",
            "unit_not_active",
            "unit_job_pending",
            "container_running",
            "container_healthy",
            "container_not_running",
            "container_absent",
            "container_unhealthy",
            "container_health_starting",
            "container_has_no_healthcheck",
            "probe_target_not_exact",
            "probe_target_ambiguous",
            "malformed_output",
            "command_failed",
            "command_timed_out",
            "docker_daemon_unavailable",
            "guest_unavailable",
            "guest_operational_confirmed",
            "settling_budget_exhausted",
        }
    )
    assert produced <= HOST_PROBE_REASONS, produced - HOST_PROBE_REASONS


def test_backend_eligibility_and_standalone_helper_grammars_are_identical() -> None:
    """Issuance must never accept a target the standalone helper refuses."""

    from app.inventory.health_execution import (
        DOCKER_NAME_PATTERN,
        SYSTEMD_UNIT_PATTERN,
        SYSTEMD_UNIT_SUFFIXES,
    )

    assert helper.SYSTEMD_UNIT_RE.pattern == SYSTEMD_UNIT_PATTERN
    assert helper.SYSTEMD_UNIT_SUFFIXES == SYSTEMD_UNIT_SUFFIXES
    assert helper.DOCKER_NAME_RE.pattern == DOCKER_NAME_PATTERN


# ===========================================================================
# discover_health_candidates (v20): a SECOND typed read-only operation on
# this same boundary. Ephemeral -- this file persists nothing, ever.
# ===========================================================================


class DiscoveryGuest:
    """A deterministic stand-in for discovery's own batched commands --
    distinct from `FakeGuest` above (different argv shapes entirely)."""

    def __init__(self) -> None:
        self.vmid = VMID
        self.node = NODE
        self.current_node = NODE
        self.present = True
        self.running = True
        self.resource_type = "lxc"
        self.docker_daemon_up = True
        #: name -> (status, "healthcheck"|"none")
        self.docker_info: dict[str, tuple[str, str]] = {}
        #: (unit, unit-file-state)
        self.unit_files: list[tuple[str, str]] = []
        self.failed_units: list[str] = []
        #: unit -> property dict; defaults to a plausible package-installed
        #: enabled unit if not overridden.
        self.unit_props: dict[str, dict[str, str]] = {}
        self.commands: list[tuple[str, ...]] = []
        #: Docker inspect completeness-proof overrides (PR #80 review
        #: finding 2/3A). When set, `_docker` answers verbatim with these
        #: instead of synthesizing a clean row per `docker_info` entry.
        self.docker_inspect_returncode = 0
        self.docker_inspect_raw_stdout: bytes | None = None
        #: systemd completeness-proof overrides (PR #80 review, systemd
        #: discovery completeness). When set, the corresponding command
        #: answers verbatim with this instead of synthesizing clean rows
        #: from `unit_files`/`unit_props` -- for shapes those structured
        #: fields cannot themselves produce (too few columns, a duplicate
        #: property key, a non-`key=value` line).
        self.unit_files_raw_stdout: bytes | None = None
        self.show_raw_stdout: bytes | None = None
        #: Same virtual-clock consumption support as `FakeGuest`, for the
        #: discovery deadline's own fresh-per-command timeout regression.
        self.clock: FakeClock | None = None
        self.consume_seconds_by_call_index: dict[int, float] = {}
        self.call_index = 0
        self.call_timeouts: list[float] = []

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
                json.dumps([{"type": "node", "name": self.node, "local": 1}]).encode()
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
        assert tail[0] == "env" and tail[1] == "LC_ALL=C", tail
        command = tail[2]
        if command == "docker":
            return self._docker(tail)
        if command == "systemctl":
            return self._systemctl(tail)
        raise AssertionError(f"unexpected guest command: {tail}")

    def _docker(self, tail):
        if tail[3] == "ps":
            if not self.docker_daemon_up:
                return helper.CommandResult(1, b"", b"daemon down")
            return self._ok(
                b"".join(
                    json.dumps(name).encode() + b"\n" for name in self.docker_info
                )
            )
        assert tail[3] == "inspect", tail
        assert tail[7] == helper._DOCKER_DISCOVERY_INSPECT_FORMAT, tail
        requested = tail[9:]
        if self.docker_inspect_raw_stdout is not None:
            return helper.CommandResult(
                self.docker_inspect_returncode, self.docker_inspect_raw_stdout, b""
            )
        lines = []
        for name in requested:
            if name in self.docker_info:
                status, hc = self.docker_info[name]
                lines.append(f"/{name}\t{status}\t{hc}")
        stdout = ("\n".join(lines) + "\n").encode() if lines else b""
        return helper.CommandResult(self.docker_inspect_returncode, stdout, b"")

    def _systemctl(self, tail):
        if tail[3] == "list-unit-files":
            assert "--plain" in tail, tail
            if self.unit_files_raw_stdout is not None:
                return self._ok(self.unit_files_raw_stdout)
            lines = [f"{unit}    {state}    -" for unit, state in self.unit_files]
            return self._ok(("\n".join(lines) + "\n").encode())
        if tail[3] == "list-units":
            assert "--plain" in tail, tail
            return self._ok(
                ("\n".join(self.failed_units) + "\n").encode()
                if self.failed_units
                else b""
            )
        assert tail[3] == "show", tail
        if self.show_raw_stdout is not None:
            return self._ok(self.show_raw_stdout)
        requested = tail[tail.index("--") + 1 :]
        blocks = []
        for unit in requested:
            props = self.unit_props.get(
                unit,
                {
                    "Id": unit,
                    "LoadState": "loaded",
                    "ActiveState": "active",
                    "UnitFileState": "enabled",
                    "FragmentPath": f"/usr/lib/systemd/system/{unit}",
                },
            )
            blocks.append("\n".join(f"{key}={value}" for key, value in props.items()))
        return self._ok(("\n\n".join(blocks) + "\n").encode())

    @staticmethod
    def _ok(stdout: bytes):
        return helper.CommandResult(0, stdout, b"")


def _discover(guest) -> dict:
    return helper.discover_health_candidates(
        guest, guest.vmid, guest.current_node, guest.node
    )


def test_docker_healthcheck_candidates_are_recommended_over_everything() -> None:
    guest = DiscoveryGuest()
    guest.docker_info = {
        "web": ("running", "healthcheck"),
        "redis": ("running", "none"),
    }
    guest.unit_files = [("nginx.service", "enabled")]
    result = _discover(guest)
    assert result["status"] == "ok"
    assert result["recommendation_basis"] == "docker_healthcheck"
    by_target = {c["target"]: c for c in result["candidates"]}
    assert by_target["web"]["kind"] == "docker_container_healthy"
    assert by_target["web"]["recommended"] is True
    # A Docker container without a HEALTHCHECK is still a real candidate --
    # candidate existence is separate from what gets recommended.
    assert by_target["redis"]["kind"] == "docker_container_running"
    assert by_target["redis"]["recommended"] is False
    # Docker HEALTHCHECK candidates outrank systemd entirely.
    assert by_target["nginx.service"]["recommended"] is False


def test_docker_running_candidates_recommended_when_none_healthchecked() -> None:
    guest = DiscoveryGuest()
    guest.docker_info = {"redis": ("running", "none"), "web": ("exited", "none")}
    result = _discover(guest)
    assert result["recommendation_basis"] == "docker_running"
    assert all(c["recommended"] for c in result["candidates"])
    assert all(c["kind"] == "docker_container_running" for c in result["candidates"])


def test_a_single_systemd_workload_candidate_is_recommended() -> None:
    guest = DiscoveryGuest()
    guest.unit_files = [
        ("mariadb.service", "enabled"),
        ("ssh.service", "enabled"),
        ("docker.service", "enabled"),
    ]
    guest.unit_props = {
        "mariadb.service": {
            "Id": "mariadb.service",
            "LoadState": "loaded",
            "ActiveState": "active",
            "UnitFileState": "enabled",
            "FragmentPath": "/usr/lib/systemd/system/mariadb.service",
        },
    }
    result = _discover(guest)
    assert result["status"] == "ok"
    assert result["recommendation_basis"] == "single_systemd_candidate"
    by_target = {c["target"]: c for c in result["candidates"]}
    assert by_target["mariadb.service"]["recommended"] is True
    assert by_target["mariadb.service"]["role_hint"] == "workload_candidate"
    assert by_target["mariadb.service"]["origin"] == "package_unit"
    # Package origin does NOT imply platform -- confirmed by the assertion
    # above staying "workload_candidate" despite a /usr/lib FragmentPath.
    assert by_target["ssh.service"]["role_hint"] == "platform"
    assert by_target["ssh.service"]["recommended"] is False
    assert by_target["docker.service"]["role_hint"] == "runtime"
    assert by_target["docker.service"]["recommended"] is False


def test_multiple_systemd_workload_candidates_are_ambiguous() -> None:
    guest = DiscoveryGuest()
    guest.unit_files = [
        ("mariadb.service", "enabled"),
        ("mosquitto.service", "enabled"),
    ]
    result = _discover(guest)
    assert result["status"] == "ambiguous_candidates"
    assert result["recommendation_basis"] is None
    assert all(not c["recommended"] for c in result["candidates"])
    assert {c["target"] for c in result["candidates"]} == {
        "mariadb.service",
        "mosquitto.service",
    }


def test_a_failed_package_service_is_still_a_workload_candidate() -> None:
    """A failed unit is discovered through the union with `list-units
    --state=failed`, even when its unit file is not separately enabled."""

    guest = DiscoveryGuest()
    guest.unit_files = []
    guest.failed_units = ["mosquitto.service loaded failed failed Mosquitto"]
    guest.unit_props = {
        "mosquitto.service": {
            "Id": "mosquitto.service",
            "LoadState": "loaded",
            "ActiveState": "failed",
            "UnitFileState": "enabled",
            "FragmentPath": "/usr/lib/systemd/system/mosquitto.service",
        },
    }
    result = _discover(guest)
    assert result["status"] == "ok"
    assert result["recommendation_basis"] == "single_systemd_candidate"
    assert result["candidates"][0]["target"] == "mosquitto.service"
    assert result["candidates"][0]["observed_state"] == "failed"


def test_no_workload_candidates_recommends_the_guest_fallback() -> None:
    """Platform-only systemd units (ssh, cron) are not a meaningful
    workload -- the fallback is recommended, and those units still appear
    in the response for operator visibility, just never recommended."""

    guest = DiscoveryGuest()
    guest.unit_files = [("ssh.service", "enabled"), ("cron.service", "enabled")]
    result = _discover(guest)
    assert result["status"] == "no_candidates"
    assert result["recommendation_basis"] == "guest_fallback"
    guest_candidates = [c for c in result["candidates"] if c["adapter"] == "guest"]
    assert guest_candidates == [
        {
            "adapter": "guest",
            "kind": "guest_operational",
            "target": None,
            "observed_state": "unknown",
            "origin": None,
            "role_hint": "workload_candidate",
            "recommended": True,
            "rationale": "guest_fallback",
        }
    ]
    platform_targets = {
        c["target"] for c in result["candidates"] if c["adapter"] == "systemd"
    }
    assert platform_targets == {"ssh.service", "cron.service"}
    assert all(
        not c["recommended"] for c in result["candidates"] if c["adapter"] == "systemd"
    )


def test_docker_uncertainty_never_produces_the_guest_fallback() -> None:
    """CRITICAL frozen rule: discovery uncertainty in EITHER family must
    never be read as "no workload exists"."""

    guest = DiscoveryGuest()
    guest.docker_daemon_up = False
    guest.unit_files = []  # systemd discovery alone would otherwise be "no
    # workload" -- must not matter once Docker is undecidable.
    result = _discover(guest)
    assert result["status"] == "undecidable"
    assert result["recommendation_basis"] is None
    assert result["candidates"] == []


def test_systemd_command_failure_never_produces_the_guest_fallback() -> None:
    guest = DiscoveryGuest()
    original = guest._systemctl

    def broken(tail):
        if tail[3] == "list-unit-files":
            return helper.CommandResult(1, b"", b"boom")
        return original(tail)

    guest._systemctl = broken
    result = _discover(guest)
    assert result["status"] == "undecidable"
    assert result["recommendation_basis"] is None


# ===========================================================================
# PR #80 review finding 2/3A: Docker discovery completeness proof. Once
# `docker ps` positively enumerates a set of names, the batched `docker
# inspect` must prove it covered EXACTLY that set -- no fewer (a name
# silently missing), no more (an unexpected name), never twice (a
# duplicate), and no malformed/unrecognised line -- or the whole family is
# undecidable. A partial accept here is exactly the "uncertainty read as
# absence" the frozen architecture forbids, and could otherwise let a real
# workload container go undiscovered while systemd alone recommends the
# guest_operational fallback.
# ===========================================================================


def test_docker_inspect_partial_stdout_with_nonzero_rc_is_undecidable() -> None:
    guest = DiscoveryGuest()
    guest.docker_info = {"web": ("running", "none"), "redis": ("running", "none")}
    guest.docker_inspect_returncode = 1
    guest.docker_inspect_raw_stdout = b"/web\trunning\tnone\n"  # redis missing
    result = _discover(guest)
    assert result["status"] == "undecidable"
    assert result["candidates"] == []
    assert result["recommendation_basis"] is None


def test_docker_inspect_empty_stdout_with_nonzero_rc_is_undecidable() -> None:
    guest = DiscoveryGuest()
    guest.docker_info = {"web": ("running", "none")}
    guest.docker_inspect_returncode = 1
    guest.docker_inspect_raw_stdout = b""
    result = _discover(guest)
    assert result["status"] == "undecidable"


def test_docker_inspect_missing_one_enumerated_name_with_zero_rc_is_undecidable() -> None:
    """Even a CLEAN (rc=0) answer that silently omits one enumerated name
    must never be accepted as a complete batch."""

    guest = DiscoveryGuest()
    guest.docker_info = {"web": ("running", "none"), "redis": ("running", "none")}
    guest.docker_inspect_returncode = 0
    guest.docker_inspect_raw_stdout = b"/web\trunning\tnone\n"  # redis missing
    result = _discover(guest)
    assert result["status"] == "undecidable"


def test_docker_inspect_duplicate_resolved_name_is_undecidable() -> None:
    guest = DiscoveryGuest()
    guest.docker_info = {"web": ("running", "none")}
    guest.docker_inspect_raw_stdout = b"/web\trunning\tnone\n/web\trunning\tnone\n"
    result = _discover(guest)
    assert result["status"] == "undecidable"


def test_docker_inspect_unexpected_name_is_undecidable() -> None:
    guest = DiscoveryGuest()
    guest.docker_info = {"web": ("running", "none")}
    guest.docker_inspect_raw_stdout = (
        b"/web\trunning\tnone\n/ghost\trunning\tnone\n"
    )
    result = _discover(guest)
    assert result["status"] == "undecidable"


def test_docker_inspect_malformed_line_is_undecidable() -> None:
    guest = DiscoveryGuest()
    guest.docker_info = {"web": ("running", "none")}
    guest.docker_inspect_raw_stdout = b"/web\trunning\n"  # missing 3rd field
    result = _discover(guest)
    assert result["status"] == "undecidable"


def test_docker_inspect_unrecognised_status_token_is_undecidable() -> None:
    guest = DiscoveryGuest()
    guest.docker_info = {"web": ("running", "none")}
    guest.docker_inspect_raw_stdout = b"/web\tsome-bogus-status\tnone\n"
    result = _discover(guest)
    assert result["status"] == "undecidable"


def test_docker_inspect_unrecognised_healthcheck_marker_is_undecidable() -> None:
    guest = DiscoveryGuest()
    guest.docker_info = {"web": ("running", "none")}
    guest.docker_inspect_raw_stdout = b"/web\trunning\tbogus\n"
    result = _discover(guest)
    assert result["status"] == "undecidable"


def test_docker_inspect_exact_complete_batch_is_positively_accepted() -> None:
    """Positive control: a clean, complete, exactly-covering batch is still
    accepted -- the completeness proof does not make ordinary discovery
    stricter than it needs to be."""

    guest = DiscoveryGuest()
    guest.docker_info = {"web": ("running", "healthcheck"), "redis": ("running", "none")}
    result = _discover(guest)
    assert result["status"] == "ok"
    by_target = {c["target"]: c for c in result["candidates"]}
    assert by_target["web"]["kind"] == "docker_container_healthy"
    assert by_target["redis"]["kind"] == "docker_container_running"


# ===========================================================================
# PR #80 review finding 3B: `systemctl list-units --state=failed` prefixes a
# failed unit's row with a bullet glyph even under `--no-legend`/`--no-
# pager` (verified: systemd 257 -- see ARCHITECTURE.md). The fix requests
# `--plain` (verified to remove it) rather than special-casing the glyph in
# the parser; `DiscoveryGuest._systemctl` above already hard-asserts
# `--plain` is present on every call, so any regression that drops it fails
# every discovery test in this file, not only these two.
# ===========================================================================


def test_a_failed_unit_is_discovered_from_the_exact_plain_output_shape() -> None:
    """The exact shape real `systemctl list-units --state=failed --plain`
    was verified to produce: no leading glyph, unit name first. Before this
    fix, a parser reading `parts[0]` off the UN-plained (bulleted) shape
    would have silently dropped this exact unit."""

    guest = DiscoveryGuest()
    guest.unit_files = []
    guest.failed_units = ["mosquitto.service loaded failed failed MQTT broker"]
    guest.unit_props = {
        "mosquitto.service": {
            "Id": "mosquitto.service",
            "LoadState": "loaded",
            "ActiveState": "failed",
            "UnitFileState": "enabled",
            "FragmentPath": "/usr/lib/systemd/system/mosquitto.service",
        },
    }
    result = _discover(guest)
    assert result["status"] == "ok"
    assert result["candidates"][0]["target"] == "mosquitto.service"


def test_systemd_discovery_requests_plain_output_on_every_invocation() -> None:
    """A hard pin against ever dropping `--plain` again: both the unit-file
    enumeration and the failed-unit enumeration must request it."""

    guest = DiscoveryGuest()
    guest.unit_files = [("mariadb.service", "enabled")]
    guest.failed_units = ["mosquitto.service loaded failed failed MQTT broker"]
    _discover(guest)
    list_unit_files_calls = [
        cmd for cmd in guest.commands if "list-unit-files" in cmd
    ]
    list_units_calls = [
        cmd
        for cmd in guest.commands
        if "list-units" in cmd and "list-unit-files" not in cmd
    ]
    assert list_unit_files_calls and all("--plain" in c for c in list_unit_files_calls)
    assert list_units_calls and all("--plain" in c for c in list_units_calls)


# ===========================================================================
# PR #80 review (follow-up): systemd discovery still lacked the SAME positive
# completeness proof the Docker half already has. Uncertain/malformed/raced
# observations were silently "continue"d rather than making the family
# undecidable -- exactly the "uncertainty read as absence" the frozen
# architecture forbids, and it could still let a false guest_operational
# fallback through.
# ===========================================================================


def test_a_malformed_list_unit_files_row_is_undecidable() -> None:
    """Witness A: a non-empty row this file cannot parse into a unit AND a
    state is never silently treated as 'no relevant unit here'."""

    guest = DiscoveryGuest()
    guest.unit_files_raw_stdout = b"mariadb.service\n"  # no state column
    result = _discover(guest)
    assert result["status"] == "undecidable"
    assert result["candidates"] == []
    assert result["recommendation_basis"] is None


def test_an_unrecognised_list_unit_files_state_token_is_undecidable() -> None:
    """A state token outside the full known systemd vocabulary is never a
    legitimate exclusion this file can vouch for."""

    guest = DiscoveryGuest()
    guest.unit_files_raw_stdout = b"mariadb.service    some-bogus-state    -\n"
    result = _discover(guest)
    assert result["status"] == "undecidable"


def test_a_malformed_failed_unit_row_is_undecidable() -> None:
    """Witness B: `--plain` is retained (asserted by the fake dispatch
    itself); a truthful failed-unit row still needs at least UNIT LOAD
    ACTIVE SUB -- fewer columns is never silently absence."""

    guest = DiscoveryGuest()
    guest.unit_files = []
    guest.failed_units = ["mosquitto.service"]  # no load/active/sub columns
    result = _discover(guest)
    assert result["status"] == "undecidable"
    assert result["candidates"] == []
    assert result["recommendation_basis"] is None


def test_a_failed_unit_row_not_actually_reporting_failed_is_undecidable() -> None:
    """`--state=failed` was requested; a row this file cannot even confirm
    as ACTIVE=failed cannot be trusted at all."""

    guest = DiscoveryGuest()
    guest.unit_files = []
    guest.failed_units = ["mosquitto.service loaded active running MQTT broker"]
    result = _discover(guest)
    assert result["status"] == "undecidable"


def test_an_enumerated_unit_that_disappears_before_show_is_undecidable() -> None:
    """Witness C. `mariadb.service` was positively enumerated as `enabled`
    a moment ago; `systemctl show` now reporting `LoadState=not-found` is a
    read-race, never proof the unit does not exist -- the whole family goes
    undecidable, not a silently shrunk (empty) candidate set."""

    guest = DiscoveryGuest()
    guest.unit_files = [("mariadb.service", "enabled")]
    guest.unit_props = {
        "mariadb.service": {
            "Id": "mariadb.service",
            "LoadState": "not-found",
            "ActiveState": "inactive",
            "UnitFileState": "",
            "FragmentPath": "",
        },
    }
    result = _discover(guest)
    assert result["status"] == "undecidable"
    assert result["candidates"] == []
    assert result["recommendation_basis"] is None


def test_a_show_block_missing_a_required_property_is_undecidable() -> None:
    """Witness D."""

    guest = DiscoveryGuest()
    guest.unit_files = [("mariadb.service", "enabled")]
    guest.show_raw_stdout = (
        b"Id=mariadb.service\nLoadState=loaded\nActiveState=active\n"
        # UnitFileState and FragmentPath omitted entirely.
    )
    result = _discover(guest)
    assert result["status"] == "undecidable"


def test_a_show_block_with_a_duplicate_property_is_undecidable() -> None:
    """Witness E. Never "first value wins" for a duplicate key."""

    guest = DiscoveryGuest()
    guest.unit_files = [("mariadb.service", "enabled")]
    guest.show_raw_stdout = (
        b"Id=mariadb.service\nLoadState=loaded\nLoadState=not-found\n"
        b"ActiveState=active\nUnitFileState=enabled\n"
        b"FragmentPath=/usr/lib/systemd/system/mariadb.service\n"
    )
    result = _discover(guest)
    assert result["status"] == "undecidable"


def test_a_show_block_with_a_malformed_line_is_undecidable() -> None:
    """Witness F."""

    guest = DiscoveryGuest()
    guest.unit_files = [("mariadb.service", "enabled")]
    guest.show_raw_stdout = (
        b"Id=mariadb.service\nthis line has no equals sign\n"
        b"LoadState=loaded\nActiveState=active\nUnitFileState=enabled\n"
        b"FragmentPath=/usr/lib/systemd/system/mariadb.service\n"
    )
    result = _discover(guest)
    assert result["status"] == "undecidable"


def test_docker_complete_empty_plus_systemd_uncertain_never_reaches_guest_fallback() -> None:
    """Witness G, the overall fallback witness: Docker positively completes
    with nothing found, but systemd's own observation is malformed --
    combined, the answer must be undecidable, never a fabricated
    guest_operational recommendation."""

    guest = DiscoveryGuest()
    guest.docker_info = {}  # Docker positively complete, zero candidates
    guest.unit_files = [("mariadb.service", "enabled")]
    guest.unit_props = {
        "mariadb.service": {
            "Id": "mariadb.service",
            "LoadState": "not-found",
            "ActiveState": "inactive",
            "UnitFileState": "",
            "FragmentPath": "",
        },
    }
    result = _discover(guest)
    assert result["status"] == "undecidable"
    assert result["recommendation_basis"] is None
    assert not any(c["adapter"] == "guest" for c in result["candidates"])


# ---------------------------------------------------------------------------
# Positive controls: the completeness proof must not make ordinary, valid
# discovery stricter than it needs to be.
# ---------------------------------------------------------------------------


def test_positive_control_normal_package_service_discovery_still_succeeds() -> None:
    guest = DiscoveryGuest()
    guest.unit_files = [("mariadb.service", "enabled")]
    result = _discover(guest)
    assert result["status"] == "ok"
    assert result["candidates"][0]["target"] == "mariadb.service"
    assert result["candidates"][0]["role_hint"] == "workload_candidate"


def test_positive_control_a_valid_failed_unit_is_still_discovered() -> None:
    guest = DiscoveryGuest()
    guest.unit_files = []
    guest.failed_units = ["mosquitto.service loaded failed failed MQTT broker"]
    guest.unit_props = {
        "mosquitto.service": {
            "Id": "mosquitto.service",
            "LoadState": "loaded",
            "ActiveState": "failed",
            "UnitFileState": "enabled",
            "FragmentPath": "/usr/lib/systemd/system/mosquitto.service",
        },
    }
    result = _discover(guest)
    assert result["status"] == "ok"
    assert result["candidates"][0]["target"] == "mosquitto.service"


def test_positive_control_platform_only_units_never_become_recommendations() -> None:
    guest = DiscoveryGuest()
    guest.unit_files = [("ssh.service", "enabled"), ("cron.service", "enabled")]
    result = _discover(guest)
    assert result["status"] == "no_candidates"
    assert result["recommendation_basis"] == "guest_fallback"
    assert all(
        not c["recommended"] for c in result["candidates"] if c["adapter"] == "systemd"
    )


def test_positive_control_a_genuinely_bare_guest_still_gets_the_guest_fallback() -> None:
    """Load-bearing: fixing uncertainty-as-absence must not disable the
    fallback globally. Positively complete Docker (nothing) and positively
    complete systemd (nothing but platform units) together still recommend
    `guest_operational`."""

    guest = DiscoveryGuest()
    guest.docker_info = {}
    guest.unit_files = [("ssh.service", "enabled")]
    result = _discover(guest)
    assert result["status"] == "no_candidates"
    assert result["recommendation_basis"] == "guest_fallback"
    guest_candidates = [c for c in result["candidates"] if c["adapter"] == "guest"]
    assert len(guest_candidates) == 1
    assert guest_candidates[0]["kind"] == "guest_operational"
    assert guest_candidates[0]["recommended"] is True


# ===========================================================================
# PR #80 review finding 3C: guest_operational may be recommended only after
# BOTH families positively and completely finished. Combining a partial
# (undecidable) Docker batch with an otherwise-empty systemd read proves
# the fallback is never reached merely because ONE family happened to look
# empty.
# ===========================================================================


def test_a_partial_docker_batch_with_empty_systemd_is_undecidable_never_guest_fallback() -> None:
    guest = DiscoveryGuest()
    guest.docker_info = {"web": ("running", "none")}
    guest.docker_inspect_returncode = 1
    guest.docker_inspect_raw_stdout = b""
    guest.unit_files = []  # systemd alone would otherwise recommend the
    # guest_operational fallback -- must not matter once Docker is
    # undecidable.
    result = _discover(guest)
    assert result["status"] == "undecidable"
    assert result["recommendation_basis"] is None
    assert result["candidates"] == []


def test_docker_discovery_bound_is_enforced() -> None:
    guest = DiscoveryGuest()
    guest.docker_info = {
        f"c{i}": ("running", "none") for i in range(helper.MAX_DOCKER_DISCOVERY_NAMES + 1)
    }
    result = _discover(guest)
    assert result["status"] == "too_many_candidates"
    assert result["candidates"] == []


def test_no_candidates_at_all_is_distinct_from_guest_fallback_status() -> None:
    """An entirely bare guest (no Docker, no discoverable systemd units at
    all) still recommends the fallback -- "no candidates" is not a
    terminal refusal, it is exactly when the fallback applies."""

    guest = DiscoveryGuest()
    result = _discover(guest)
    assert result["recommendation_basis"] == "guest_fallback"


def test_a_guest_that_is_not_running_is_reported_not_faked_as_bare() -> None:
    guest = DiscoveryGuest()
    guest.running = False
    response = helper.handle_discover_request(
        {
            "request_version": 1,
            "operation": "discover_health_candidates",
            "target": {"vmid": VMID, "expected_node": NODE},
            "ownership": {
                "resource_id": str(uuid.uuid4()),
                "binding_id": str(uuid.uuid4()),
                "locator_generation": 1,
                "resource_continuity_revision": 1,
                "backend_instance_id": str(uuid.uuid4()),
            },
        },
        runner=guest,
    )
    assert response["ok"] is True
    assert response["discovery_status"] == "guest_unavailable"
    assert response["candidates"] == []


def test_discovery_request_never_accepts_a_job_id_or_workload_selector() -> None:
    payload = {
        "request_version": 1,
        "operation": "discover_health_candidates",
        "target": {"vmid": VMID, "expected_node": NODE},
        "ownership": {
            "resource_id": str(uuid.uuid4()),
            "binding_id": str(uuid.uuid4()),
            "locator_generation": 1,
            "resource_continuity_revision": 1,
            "backend_instance_id": str(uuid.uuid4()),
            "job_id": str(uuid.uuid4()),
        },
    }
    with pytest.raises(helper.RequestError, match="exact discovery shape"):
        helper.validate_discover_request(payload)


def test_the_evaluate_acceptance_marker_is_unaffected_by_discovery() -> None:
    """The byte-identical bootstrap/updater acceptance marker still fires
    for anything that is not literally the new operation -- including an
    entirely empty payload, which `handle_request`'s dispatch must still
    route to the SAME existing evaluate-request validation."""

    with pytest.raises(
        helper.RequestError, match="request must have the exact health-evaluation shape"
    ):
        helper.validate_request({})
    with pytest.raises(
        helper.RequestError, match="request must have the exact health-evaluation shape"
    ):
        helper.handle_request({}, runner=DiscoveryGuest())
