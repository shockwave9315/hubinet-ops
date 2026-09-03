"""The dark health helper's fixed argv, and every way it must not false-PASS.

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
- `docker inspect` resolves by ID prefix as well as by name;
- `docker inspect` cannot distinguish "no such container" from "daemon
  unavailable" by exit code.

Nothing here runs a real `pvesh`, `pct`, `ssh`, `systemctl`, or `docker`.
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
        #: unit id -> (LoadState, ActiveState)
        self.units: dict[str, tuple[str, str]] = {
            "nginx.service": ("loaded", "active"),
            "worker.service": ("loaded", "failed"),
        }
        #: Alias -> canonical unit id, exactly as systemd resolves one.
        self.unit_aliases: dict[str, str] = {}
        #: container name -> (running, health) with health "<none>" for a
        #: container that declares no HEALTHCHECK.
        self.containers: dict[str, tuple[bool, str]] = {
            "web": (True, "healthy"),
        }
        self.docker_daemon_up = True
        self.systemctl_returncode = 0
        self.systemctl_stdout_override: str | None = None
        self.commands: list[tuple[str, ...]] = []
        self.timeout_on: str | None = None

    # -- host commands -------------------------------------------------

    def __call__(self, argv, timeout, max_output):
        self.commands.append(tuple(argv))
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
        assert tail[0] == "env" and tail[1] == "LC_ALL=C", tail
        command = tail[2]
        if command == "systemctl":
            return self._systemctl(tail)
        if command == "docker":
            return self._docker(tail)
        raise AssertionError(f"unexpected guest command: {tail}")

    # -- systemd -------------------------------------------------------

    def _systemctl(self, tail):
        assert tail[3] == "show", tail
        assert "--no-pager" in tail, tail
        # Everything after the end-of-options marker is a unit name, never an
        # option. There is exactly one, and it is the last element.
        assert tail[-2] == "--", tail
        assert "is-active" not in tail, "is-active can pass on another unit"
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
        requested = tail[-1]
        matched = self._match_units(requested)
        if not matched:
            # systemd answers for a unit that does not exist with a normal
            # success and LoadState=not-found.
            blocks = [
                f"Id={requested}\nLoadState=not-found\nActiveState=inactive"
            ]
        else:
            blocks = [
                f"Id={unit}\nLoadState={self.units[unit][0]}\n"
                f"ActiveState={self.units[unit][1]}"
                for unit in matched
            ]
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
            if not self.docker_daemon_up:
                return helper.CommandResult(
                    1, b"", b"Cannot connect to the Docker daemon"
                )
            if tuple(tail[3:]) == ("ps", "--all", "--no-trunc", "--quiet"):
                return self._ok(b"")
            assert tuple(tail[3:]) == (
                "ps",
                "--all",
                "--no-trunc",
                "--format",
                helper.DOCKER_NAME_LIST_FORMAT,
            ), tail
            return self._ok(
                b"".join(
                    json.dumps(name).encode() + b"\n"
                    for name in self.containers
                )
            )
        assert tail[3] == "inspect", tail
        assert tuple(tail[4:6]) == ("--type", "container"), tail
        assert tail[6] == helper.DOCKER_INSPECT_FORMAT_FLAG, tail
        # The template is a CONSTANT owned by the helper, byte for byte.
        assert tail[7] == helper.DOCKER_INSPECT_FORMAT, tail
        assert tail[8] == "--", tail
        assert len(tail) == 10, tail
        if self.timeout_on == "docker":
            return helper.CommandResult(0, b"", b"", timed_out=True)
        if not self.docker_daemon_up:
            return helper.CommandResult(
                1, b"", b"Cannot connect to the Docker daemon"
            )
        requested = tail[-1]
        resolved = self._resolve_container(requested)
        if resolved is None:
            return helper.CommandResult(
                1, b"", f"Error response from daemon: No such container: {requested}".encode()
            )
        name, (running, health) = resolved
        line = f"/{name}\t{'true' if running else 'false'}\t{health}\n"
        return self._ok(line.encode())

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
    }


def _evaluate(guest, probes, *, node: str = NODE):
    response = helper.handle_request(_request(probes, node=node), runner=guest)
    assert response["ok"] is True, response
    assert response["health_contract"] == {"revision": 4, "fingerprint": "a" * 64}
    return [(probe["outcome"], probe["reason"]) for probe in response["probes"]]


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
        "--",
        "nginx.service",
    )


def test_a_known_inactive_unit_is_a_definitive_failure() -> None:
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
    # It never even reached the guest.
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


def test_multiple_property_blocks_are_ambiguous_never_a_pass() -> None:
    """Belt and braces under the charset check: if a target ever did match
    more than one unit, the answer says nothing about the requested one."""

    guest = FakeGuest()
    guest.systemctl_stdout_override = (
        "Id=nginx.service\nLoadState=loaded\nActiveState=active\n\n"
        "Id=other.service\nLoadState=loaded\nActiveState=active\n"
    )
    assert _evaluate(guest, (("systemd_unit_active", "nginx.service"),)) == [
        ("unknown", "probe_target_ambiguous")
    ]


@pytest.mark.parametrize(
    "stdout",
    (
        "",
        "ActiveState=active\n",
        "Id=nginx.service\nActiveState=active\n",
        "Id=nginx.service\nLoadState=loaded\nActiveState=active\nExtra=1\n",
        "Id=nginx.service\nLoadState=loaded\nActiveState=quantum\n",
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


def test_a_stopped_container_is_a_definitive_failure() -> None:
    guest = FakeGuest()
    guest.containers["web"] = (False, "<none>")
    assert _evaluate(guest, (("docker_container_running", "web"),)) == [
        ("failed", "container_not_running")
    ]


def test_an_absent_container_is_a_failure_only_because_the_daemon_answered() -> None:
    guest = FakeGuest()
    assert _evaluate(guest, (("docker_container_running", "gone"),)) == [
        ("failed", "container_absent")
    ]
    # The daemon oracle ran before inspect, and the fixed exact-name inventory
    # positively proved the requested name absent afterwards.
    oracles = [argv for argv in guest.commands if "ps" in argv]
    assert len(oracles) == 2
    assert oracles[-1][-2:] == (
        "--format",
        helper.DOCKER_NAME_LIST_FORMAT,
    )


def test_a_docker_daemon_that_is_down_is_unknown_never_absent() -> None:
    guest = FakeGuest()
    guest.docker_daemon_up = False
    assert _evaluate(guest, (("docker_container_running", "web"),)) == [
        ("unknown", "docker_daemon_unavailable")
    ]


def test_an_id_prefix_resolution_can_never_produce_a_pass() -> None:
    """`docker inspect` resolves by ID prefix, so a hex-looking target could
    otherwise pass because it prefixed some OTHER container's id."""

    guest = FakeGuest()
    prefix = _fake_container_id("web")[:12]
    assert _evaluate(guest, (("docker_container_running", prefix),)) == [
        ("unknown", "probe_target_not_exact")
    ]


@pytest.mark.parametrize("target", ("--help", "-f", "/web", "web name"))
def test_an_option_or_path_like_container_target_is_refused(target: str) -> None:
    guest = FakeGuest()
    assert _evaluate(guest, (("docker_container_running", target),)) == [
        ("unknown", "probe_target_not_exact")
    ]


def test_malformed_inspect_output_is_unknown() -> None:
    guest = FakeGuest()
    guest.containers["web"] = (True, "healthy")

    original = guest._docker

    def broken(tail):
        result = original(tail)
        if "inspect" in tail:
            return helper.CommandResult(0, b"/web\tmaybe\thealthy\n", b"")
        return result

    guest._docker = broken
    assert _evaluate(guest, (("docker_container_running", "web"),)) == [
        ("unknown", "malformed_output")
    ]


def test_an_inspect_timeout_with_a_realistic_killed_returncode_is_unknown() -> None:
    guest = FakeGuest()
    original = guest._docker

    def timed_out(tail):
        if tail[3] == "inspect":
            return helper.CommandResult(-9, b"", b"", timed_out=True)
        return original(tail)

    guest._docker = timed_out
    assert _evaluate(guest, (("docker_container_running", "web"),)) == [
        ("unknown", "command_timed_out")
    ]


def test_an_inspect_output_overflow_with_nonzero_returncode_is_unknown() -> None:
    guest = FakeGuest()
    original = guest._docker

    def overflowed(tail):
        if tail[3] == "inspect":
            return helper.CommandResult(-9, b"x", b"", output_exceeded=True)
        return original(tail)

    guest._docker = overflowed
    assert _evaluate(guest, (("docker_container_running", "web"),)) == [
        ("unknown", "malformed_output")
    ]


def test_a_generic_inspect_failure_is_not_absence_while_name_still_exists() -> None:
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


@pytest.mark.parametrize(
    ("listing_result", "reason"),
    (
        (
            helper.CommandResult(1, b"", b"daemon unavailable"),
            "docker_daemon_unavailable",
        ),
        (helper.CommandResult(-9, b"", b"", timed_out=True), "command_timed_out"),
        (helper.CommandResult(-9, b"x", b"", output_exceeded=True), "malformed_output"),
        (helper.CommandResult(0, b"not-json\n", b""), "malformed_output"),
    ),
)
def test_an_unusable_exact_name_absence_proof_is_unknown(
    listing_result, reason: str
) -> None:
    guest = FakeGuest()
    original = guest._docker

    def unusable_listing(tail):
        if tail[3] == "ps" and "--format" in tail:
            return listing_result
        return original(tail)

    guest._docker = unusable_listing
    assert _evaluate(guest, (("docker_container_running", "gone"),)) == [
        ("unknown", reason)
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
        ((True, "unhealthy"), "container_unhealthy"),
        ((True, "starting"), "container_health_starting"),
        ((True, "<none>"), "container_has_no_healthcheck"),
        ((False, "healthy"), "container_not_running"),
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


def test_a_container_running_probe_still_passes_for_an_unhealthy_container() -> None:
    """The two Docker kinds are genuinely different questions."""

    guest = FakeGuest()
    guest.containers["web"] = (True, "unhealthy")
    assert _evaluate(guest, (("docker_container_running", "web"),)) == [
        ("passed", "container_running")
    ]


def test_an_unknown_health_status_is_unknown_not_a_guess() -> None:
    guest = FakeGuest()
    guest.containers["web"] = (True, "mysterious")
    assert _evaluate(guest, (("docker_container_healthy", "web"),)) == [
        ("unknown", "malformed_output")
    ]


# ===========================================================================
# 4. Live target revalidation, and whole-request refusals
# ===========================================================================


def test_every_guest_command_revalidates_the_live_target_first() -> None:
    """The dispatcher owns the invariant, so no caller can amortize one
    check across two commands and send the second to a replacement guest."""

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


def test_a_guest_that_moved_node_refuses_the_whole_evaluation() -> None:
    guest = FakeGuest()
    guest.current_node = "pve-b"
    response = helper.handle_request(
        _request((("systemd_unit_active", "nginx.service"),)), runner=guest
    )
    assert response["ok"] is False
    assert response["error"]["classification"] == "stale_target"


def test_a_guest_that_is_not_running_refuses_the_whole_evaluation() -> None:
    guest = FakeGuest()
    guest.running = False
    response = helper.handle_request(
        _request((("systemd_unit_active", "nginx.service"),)), runner=guest
    )
    assert response["ok"] is False
    assert response["error"]["classification"] == "guest_unavailable"


def test_a_guest_replaced_mid_evaluation_makes_that_probe_unknown() -> None:
    """A whole-host problem observed mid-probe is never a failure of the
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

    response = helper.handle_request(
        _request(
            (
                ("systemd_unit_active", "nginx.service"),
                ("docker_container_running", "web"),
            )
        ),
        runner=moving,
    )
    assert response["ok"] is True
    assert response["probes"][1]["outcome"] == "unknown"


# ===========================================================================
# 5. The request boundary
# ===========================================================================


def test_the_helper_accepts_exactly_one_request_shape() -> None:
    for payload in (
        {},
        {"request_version": 2, "operation": "evaluate_health_contract"},
        {**_request((("systemd_unit_active", "a.service"),)), "extra": 1},
    ):
        with pytest.raises(helper.RequestError):
            helper.validate_request(payload)


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
# 6. The real transport, over a real JSON round trip
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


def _round_trip_runner(guest):
    """Run the REAL helper against the fake guest, over real JSON bytes."""

    from app.package_scan_host_control import BoundedProcessResult

    def runner(argv, stdin, timeout, max_bytes):
        assert argv[0] == "ssh"
        # Pinned trust, no password, no forwarding, no interactive shell.
        assert "BatchMode=yes" in argv
        assert "StrictHostKeyChecking=yes" in argv
        assert "PasswordAuthentication=no" in argv
        assert "ForwardAgent=no" in argv
        payload = json.loads(stdin.decode("utf-8"))
        response = helper.handle_request(payload, runner=guest)
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
    guest.units["nginx.service"] = ("loaded", "active")
    guest.containers["web"] = (True, "healthy")

    result = _transport(_round_trip_runner(guest)).evaluate_health_contract(request)

    assert result.contract_revision == job.health_contract_revision
    assert result.contract_fingerprint == job.health_contract_fingerprint
    assert [(probe.probe_index, probe.outcome.value) for probe in result.probes] == [
        (0, "passed"),
        (1, "passed"),
    ]


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
# 7. Cross-node routing: the one place a command line exists
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


def test_an_element_that_would_need_quoting_is_refused_rather_than_quoted(
    monkeypatch,
) -> None:
    """The assertion is load-bearing, not decorative.

    The kind-specific validation makes such a target unreachable, so this
    reaches past it to prove the routing boundary refuses on its own rather
    than relying on a single upstream check.
    """

    guest = RemoteGuest()
    with pytest.raises(helper.ProbeUnknown, match="probe_target_not_exact"):
        helper._run_guest_command(
            guest,
            VMID,
            "pve-b",
            "pve-a",
            ("env", "LC_ALL=C", "systemctl", "show", "--", "a b.service"),
            data_argument="a b.service",
        )
    assert guest.remote_command_lines == []


def test_the_helper_can_only_report_reasons_the_backend_accepts() -> None:
    """A closed taxonomy is only closed if both ends agree on it.

    Every reason literal the helper's evaluators and unevaluable paths can
    produce must be one the transport parses and the authority stores. A
    helper token the backend rejects would silently turn a truthful probe
    result into a rejected response -- and a backend token the helper could
    never send would be dead vocabulary.
    """

    import ast

    from app.inventory import HEALTH_PROBE_REASONS
    from app.package_update_health import HOST_PROBE_REASONS

    assert HOST_PROBE_REASONS <= HEALTH_PROBE_REASONS

    source = HELPER_PATH.read_text(encoding="utf-8")
    module = ast.parse(source, filename=str(HELPER_PATH))
    produced: set[str] = set()
    for node in ast.walk(module):
        # Every unevaluable path: `raise ProbeUnknown("<token>")`.
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "ProbeUnknown"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            produced.add(node.args[0].value)
        # Every definitive verdict: `return "<outcome>", "<token>"`.
        if (
            isinstance(node, ast.Return)
            and isinstance(node.value, ast.Tuple)
            and len(node.value.elts) == 2
            and all(isinstance(part, ast.Constant) for part in node.value.elts)
        ):
            outcome, reason = (part.value for part in node.value.elts)
            if outcome in ("passed", "failed"):
                produced.add(reason)

    assert produced, "no reason tokens were found in the helper"
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
