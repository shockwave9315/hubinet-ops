from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType
import uuid

import pytest

from app.inventory import (
    PackageScanFailure,
    PackageScanLifecycle,
    PackageScanRun,
    package_plan_fingerprint,
)
from app.package_scan import (
    HostScanFailure,
    PackageScanParseError,
    classify_command_failure,
    parse_apt_simulation,
    parse_os_release,
)
from app.package_scan_host_control import (
    BoundedProcessResult,
    SshPackageScanHostControl,
)


ROOT = Path(__file__).resolve().parents[1]
HELPER_PATH = ROOT / "deploy" / "hubinet-package-scan-helper.py"


def _load_helper() -> ModuleType:
    spec = importlib.util.spec_from_file_location("hubinet_package_scan_helper", HELPER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


helper = _load_helper()


DEBIAN_SIMULATION = """\
Reading package lists...
Building dependency tree...
Reading state information...
Calculating upgrade...
Inst openssl [3.0.11-1] (3.0.11-1~deb12u3 Debian-Security:12/oldstable-security [amd64])
Inst apt [2.6.1] (2.6.2 Debian:12/oldstable [amd64])
Conf openssl (3.0.11-1~deb12u3 Debian-Security:12/oldstable-security [amd64])
Conf apt (2.6.2 Debian:12/oldstable [amd64])
2 upgraded, 0 newly installed, 0 to remove and 1 not upgraded.
"""

UBUNTU_SIMULATION = """\
Reading package lists...
Building dependency tree...
Reading state information...
Calculating upgrade...
Inst base-files [13ubuntu10.2] (13ubuntu10.3 Ubuntu:24.04/noble-updates [amd64])
1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.
"""

ZERO_SIMULATION = """\
Reading package lists...
Building dependency tree...
Reading state information...
Calculating upgrade...
0 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.
"""


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        ('ID=debian\nVERSION_ID="12"\n', ("debian", "12")),
        ('NAME="Ubuntu"\nID=ubuntu\nVERSION_ID="24.04"\n', ("ubuntu", "24.04")),
    ),
)
def test_debian_and_ubuntu_os_release_parsing(text: str, expected: tuple[str, str]) -> None:
    assert parse_os_release(text) == expected


def test_unsupported_os_is_classified_without_guessing() -> None:
    with pytest.raises(HostScanFailure) as caught:
        parse_os_release('ID=alpine\nVERSION_ID="3.20"\n')
    assert caught.value.failure_class is PackageScanFailure.UNSUPPORTED_OS


def test_zero_and_multiple_exact_apt_updates() -> None:
    assert parse_apt_simulation(ZERO_SIMULATION) == ()
    debian = parse_apt_simulation(DEBIAN_SIMULATION)
    assert tuple(item.package_name for item in debian) == ("apt", "openssl")
    assert debian[1].installed_version == "3.0.11-1"
    assert debian[1].candidate_version == "3.0.11-1~deb12u3"
    assert debian[1].security is True
    assert debian[0].security is None
    ubuntu = parse_apt_simulation(UBUNTU_SIMULATION)
    assert ubuntu[0].origin == "Ubuntu:24.04/noble-updates [amd64]"
    assert ubuntu[0].security is None


@pytest.mark.parametrize(
    (
        "change",
        "expected_name",
        "expected_installed",
        "expected_candidate",
        "expected_origin",
        "expected_security",
    ),
    (
        (
            "Inst foo [1.0] (1.1 Debian:stable [amd64])",
            "foo",
            "1.0",
            "1.1",
            "Debian:stable [amd64]",
            None,
        ),
        (
            "Inst foo [1.0] (1.1 Debian:stable [amd64]) []",
            "foo",
            "1.0",
            "1.1",
            "Debian:stable [amd64]",
            None,
        ),
        (
            "Inst liblastlog2-2 [2.41-5] "
            "(2.41.5-0+deb13u1 Debian-Security:13/stable-security [amd64]) "
            "[util-linux:amd64 on liblastlog2-2:amd64] [util-linux:amd64 ]",
            "liblastlog2-2",
            "2.41-5",
            "2.41.5-0+deb13u1",
            "Debian-Security:13/stable-security [amd64]",
            True,
        ),
        (
            "Inst foo [1.0] (1.1 Debian:stable [amd64]) "
            "[util-linux:amd64 on foo:amd64]",
            "foo",
            "1.0",
            "1.1",
            "Debian:stable [amd64]",
            None,
        ),
        (
            "Inst bind9-host [1:9.20.23-1~deb13u1] "
            "(1:9.20.26-1~deb13u1 Debian-Security:13/stable-security [amd64]) []",
            "bind9-host",
            "1:9.20.23-1~deb13u1",
            "1:9.20.26-1~deb13u1",
            "Debian-Security:13/stable-security [amd64]",
            True,
        ),
    ),
)
def test_realistic_apt_shortbreaks_suffix_is_not_material_plan_data(
    change: str,
    expected_name: str,
    expected_installed: str,
    expected_candidate: str,
    expected_origin: str,
    expected_security: bool | None,
) -> None:
    parsed = parse_apt_simulation(
        f"{change}\n1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n"
    )
    assert parsed[0].package_name == expected_name
    assert parsed[0].installed_version == expected_installed
    assert parsed[0].candidate_version == expected_candidate
    assert parsed[0].origin == expected_origin
    assert parsed[0].security is expected_security


def test_shortbreaks_suffix_does_not_change_material_fingerprint() -> None:
    plain = parse_apt_simulation(
        "Inst foo [1.0] (1.1 Debian:stable [amd64])\n"
        "1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n"
    )
    shortbreaks = parse_apt_simulation(
        "Inst foo [1.0] (1.1 Debian:stable [amd64]) "
        "[util-linux:amd64 on foo:amd64] [util-linux:amd64 ]\n"
        "1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n"
    )
    assert plain == shortbreaks
    assert package_plan_fingerprint(plain) == package_plan_fingerprint(shortbreaks)


@pytest.mark.parametrize(
    "text",
    (
        "Inst apt (2.6.2 Debian:12/stable [amd64])\n1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n",
        "Inst apt [2.6.1] broken\n1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n",
        "Inst foo [1.0] (1.1 Debian:stable [amd64]) [unclosed\n1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n",
        "Inst foo [1.0] (1.1 Debian:stable [amd64]) stray\n1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n",
        "Inst apt [2.6.1] (2.6.2 Debian:12/stable [amd64])\n0 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n",
        "Remv obsolete [1.0]\n0 upgraded, 0 newly installed, 1 to remove and 0 not upgraded.\n",
        "Reading package lists...\n",
    ),
)
def test_malformed_or_inexact_simulation_fails_scan(text: str) -> None:
    with pytest.raises(PackageScanParseError):
        parse_apt_simulation(text)


def test_package_manager_busy_has_distinct_classification() -> None:
    assert classify_command_failure(
        stage="metadata_refresh",
        returncode=100,
        stderr="E: Could not get lock /var/lib/dpkg/lock-frontend",
    ) is PackageScanFailure.PACKAGE_MANAGER_BUSY
    assert classify_command_failure(
        stage="metadata_refresh", returncode=100, stderr="repository unavailable"
    ) is PackageScanFailure.METADATA_REFRESH_FAILED


def _request(*, vmid=101, operation="scan_packages", expected_node="pve-a"):
    return {
        "request_version": 1,
        "operation": operation,
        "target": {"vmid": vmid, "expected_node": expected_node},
        "context": {
            "scan_run_id": str(uuid.uuid4()),
            "resource_id": str(uuid.uuid4()),
            "binding_id": str(uuid.uuid4()),
            "locator_generation": 2,
            "resource_continuity_revision": 3,
        },
    }


class FakeHelperRunner:
    def __init__(
        self,
        *,
        resource_type: str = "lxc",
        status: str = "running",
        node: str = "pve-a",
        local_node: str = "pve-a",
        migrate_after_checks: int | None = None,
        migrate_to_node: str = "pve-c",
        os_release: str = 'ID=debian\nVERSION_ID="12"\n',
        update_returncode: int = 0,
        update_stderr: str = "",
        simulation_returncode: int = 0,
        simulation_stderr: str = "",
        remote_returncode: int | None = None,
        remote_stderr: str = "",
        remote_failure_command: str | None = None,
        os_returncode: int = 0,
        timed_out_command: str | None = None,
    ) -> None:
        self.resource_type = resource_type
        self.status = status
        self.node = node
        self.local_node = local_node
        self.migrate_after_checks = migrate_after_checks
        self.migrate_to_node = migrate_to_node
        self.os_release = os_release
        self.update_returncode = update_returncode
        self.update_stderr = update_stderr
        self.simulation_returncode = simulation_returncode
        self.simulation_stderr = simulation_stderr
        self.remote_returncode = remote_returncode
        self.remote_stderr = remote_stderr
        self.remote_failure_command = remote_failure_command
        self.os_returncode = os_returncode
        self.timed_out_command = timed_out_command
        self.calls: list[tuple[str, ...]] = []
        self._target_checks = 0

    def __call__(self, argv, _timeout, _max_output):
        self.calls.append(argv)
        rendered = " ".join(argv)
        if self.timed_out_command and self.timed_out_command in rendered:
            return helper.CommandResult(-9, b"", b"", timed_out=True)
        if argv[0] == "pvesh" and argv[2] == "/cluster/status":
            rows = [
                {
                    "type": "node",
                    "name": self.local_node,
                    "local": 1,
                    "nodeid": 0,
                    "online": 1,
                }
            ]
            return helper.CommandResult(0, json.dumps(rows).encode(), b"")
        if argv[0] == "pvesh" and argv[2] == "/cluster/resources":
            self._target_checks += 1
            current_node = self.node
            if (
                self.migrate_after_checks is not None
                and self._target_checks > self.migrate_after_checks
            ):
                current_node = self.migrate_to_node
            rows = [
                {
                    "vmid": 101,
                    "type": self.resource_type,
                    "node": current_node,
                    "status": self.status,
                }
            ]
            return helper.CommandResult(0, json.dumps(rows).encode(), b"")
        if (
            self.remote_returncode is not None
            and argv[0] == "ssh"
            and (
                self.remote_failure_command is None
                or self.remote_failure_command in rendered
            )
        ):
            return helper.CommandResult(
                self.remote_returncode, b"", self.remote_stderr.encode()
            )
        if "/etc/os-release" in rendered:
            return helper.CommandResult(self.os_returncode, self.os_release.encode(), b"")
        if "update" in rendered and "apt-get" in rendered:
            return helper.CommandResult(
                self.update_returncode, b"", self.update_stderr.encode()
            )
        if "upgrade" in rendered:
            return helper.CommandResult(
                self.simulation_returncode,
                ZERO_SIMULATION.encode(),
                self.simulation_stderr.encode(),
            )
        if "/var/run/reboot-required" in rendered:
            return helper.CommandResult(1, b"", b"")
        raise AssertionError(f"unexpected command shape: {argv!r}")


def test_helper_accepts_only_typed_operation_and_strict_vmid() -> None:
    with pytest.raises(helper.RequestError, match="unknown"):
        helper.validate_request(_request(operation="shell"))
    for malformed in ("101", 0, -1, True, 99, 1_000_000_000):
        with pytest.raises(helper.RequestError, match="vmid"):
            helper.validate_request(_request(vmid=malformed))
    request = _request()
    request["command"] = "apt-get upgrade"
    with pytest.raises(helper.RequestError, match="exact"):
        helper.validate_request(request)


def test_helper_success_uses_only_fixed_pvesh_and_pct_shapes() -> None:
    runner = FakeHelperRunner()
    response = helper.handle_request(_request(), runner=runner)
    assert response["ok"] is True
    assert response["reboot_required"] is None
    assert all(call[0] in {"pvesh", "pct"} for call in runner.calls)
    assert any(
        call[-4:] == ("apt-get", "update", "-qq", "--error-on=any")
        for call in runner.calls
    )
    assert any(call[-3:] == ("apt-get", "-s", "upgrade") for call in runner.calls)
    assert not any("eval" in argument for call in runner.calls for argument in call)


@pytest.mark.parametrize(
    ("runner", "classification"),
    (
        (FakeHelperRunner(status="stopped"), "guest_unavailable"),
        (FakeHelperRunner(resource_type="qemu"), "unsupported_resource_type"),
        (FakeHelperRunner(node="pve-b"), "stale_target"),
        (FakeHelperRunner(os_release='ID=alpine\nVERSION_ID="3.20"\n'), "unsupported_os"),
        (
            FakeHelperRunner(update_returncode=100, update_stderr="repository unavailable"),
            "metadata_refresh_failed",
        ),
        (
            FakeHelperRunner(
                update_returncode=100,
                update_stderr="E: Could not get lock /var/lib/dpkg/lock-frontend",
            ),
            "package_manager_busy",
        ),
        (
            FakeHelperRunner(simulation_returncode=100, simulation_stderr="failed"),
            "simulation_failed",
        ),
        (FakeHelperRunner(timed_out_command="apt-get update"), "timeout"),
    ),
)
def test_helper_classifies_ordinary_failures(runner, classification: str) -> None:
    response = helper.handle_request(_request(), runner=runner)
    assert response["ok"] is False
    assert response["error"]["classification"] == classification
    assert "stdout" not in response["error"]
    assert "stderr" not in response["error"]
    if classification in {"metadata_refresh_failed", "package_manager_busy"}:
        # Corrective pass, Finding 1: a partial/failing metadata refresh
        # must never proceed to the upgrade simulation -- the resulting
        # scan must be a failure, never a successful (and possibly stale)
        # "0 updates" plan.
        assert not any("upgrade" in " ".join(call) for call in runner.calls)


def test_helper_metadata_refresh_uses_fail_on_any_error_and_never_simulates_on_failure() -> None:
    # Corrective pass, Finding 1 witness: prove (1) the actual fixed argv
    # sent to the guest carries APT's own fail-on-any-error option, and
    # (2) a refresh APT reports as failed because of it never reaches the
    # simulation stage, so the scan is a hard failure rather than a
    # successful exact plan against stale/incomplete indexes.
    runner = FakeHelperRunner(
        update_returncode=100,
        update_stderr="E: Some index files failed to download",
    )
    response = helper.handle_request(_request(), runner=runner)
    update_calls = [
        call for call in runner.calls if call[0] == "pct" and "update" in call and "apt-get" in call
    ]
    assert update_calls, "expected the fixed apt-get update argv to be issued"
    assert update_calls[0][-4:] == ("apt-get", "update", "-qq", "--error-on=any")
    assert not any("upgrade" in call for call in runner.calls)
    assert response["ok"] is False
    assert response["error"]["classification"] == "metadata_refresh_failed"


def test_helper_runs_local_node_lxc_directly_without_ssh() -> None:
    runner = FakeHelperRunner(node="pve-a", local_node="pve-a")
    response = helper.handle_request(_request(expected_node="pve-a"), runner=runner)
    assert response["ok"] is True
    assert not any(call[0] == "ssh" for call in runner.calls)
    assert any(call[0] == "pct" and call[1] == "exec" for call in runner.calls)


def test_helper_routes_remote_node_lxc_execution_over_cluster_ssh() -> None:
    # The bootstrap/entry PVE node is pve-a, but the LXC's expected (and
    # cluster-resources-confirmed) node is pve-b. Every fixed pct exec shape
    # must be routed to pve-b rather than run locally on pve-a.
    runner = FakeHelperRunner(node="pve-b", local_node="pve-a")
    response = helper.handle_request(_request(expected_node="pve-b"), runner=runner)
    assert response["ok"] is True
    assert not any(call[0] == "pct" for call in runner.calls)
    ssh_calls = [call for call in runner.calls if call[0] == "ssh"]
    assert len(ssh_calls) == 4
    for call in ssh_calls:
        assert call[-2] == "root@pve-b"
        assert "BatchMode=yes" in call
        assert "StrictHostKeyChecking=yes" in call
        remote_command = call[-1]
        assert remote_command.startswith("pct exec 101 --")
        assert "eval" not in remote_command
    assert any("apt-get update -qq" in call[-1] for call in ssh_calls)
    assert any("apt-get -s upgrade" in call[-1] for call in ssh_calls)


def test_helper_migration_between_validations_fails_closed_never_success() -> None:
    # The guest starts on the expected node but migrates to a third node
    # partway through the fixed operation sequence. The stale-target check
    # that precedes every guest operation must catch this and stop the scan
    # rather than let a later step commit success against the wrong node.
    runner = FakeHelperRunner(
        node="pve-b",
        local_node="pve-a",
        migrate_after_checks=1,
        migrate_to_node="pve-c",
    )
    response = helper.handle_request(_request(expected_node="pve-b"), runner=runner)
    assert response["ok"] is False
    assert response["error"]["classification"] == "stale_target"
    # Only the first (os-release) guest command should have been attempted.
    assert sum(1 for call in runner.calls if call[0] == "ssh") == 1


@pytest.mark.parametrize(
    "failed_command", ("/etc/os-release", "apt-get update -qq", "apt-get -s upgrade")
)
def test_helper_remote_node_transport_failure_is_execution_failed(
    failed_command: str,
) -> None:
    runner = FakeHelperRunner(
        node="pve-b",
        local_node="pve-a",
        remote_returncode=255,
        remote_stderr="ssh: connect to host pve-b port 22: Connection refused",
        remote_failure_command=failed_command,
    )
    response = helper.handle_request(_request(expected_node="pve-b"), runner=runner)
    assert response["ok"] is False
    assert response["error"]["classification"] == "execution_failed"


def test_helper_failed_os_release_retrieval_is_not_unsupported_os() -> None:
    response = helper.handle_request(_request(), runner=FakeHelperRunner(os_returncode=1))
    assert response["ok"] is False
    assert response["error"]["classification"] == "guest_unavailable"


def test_helper_rejects_expected_node_with_shell_metacharacters() -> None:
    for hostile in (
        "pve-a; rm -rf /",
        "pve-a && evil",
        "$(evil)",
        "-oProxyCommand=evil",
        "pve a",
        "pve-a\nrm -rf /",
    ):
        with pytest.raises(helper.RequestError, match="expected_node"):
            helper.validate_request(_request(expected_node=hostile))


def _scan_run() -> PackageScanRun:
    return PackageScanRun(
        scan_run_id=str(uuid.uuid4()),
        resource_id=str(uuid.uuid4()),
        inventory_source_id=str(uuid.uuid4()),
        attempt_sequence=1,
        expected_binding_id=str(uuid.uuid4()),
        expected_locator_generation=2,
        expected_resource_continuity_revision=3,
        expected_vmid=101,
        expected_node_id=str(uuid.uuid4()),
        expected_node_name="pve-a",
        started_at="2026-08-28T12:00:00+00:00",
        lifecycle=PackageScanLifecycle.RUNNING,
        completed_at=None,
        outcome=None,
        failure_class=None,
        error_message=None,
        os_id=None,
        os_version=None,
        pending_count=None,
        plan_fingerprint=None,
        reboot_required=None,
    )


def test_ssh_host_control_pins_key_and_host_key_and_sends_json_on_stdin(tmp_path: Path) -> None:
    run = _scan_run()
    captured = {}

    def runner(argv, input_bytes, timeout, max_output):
        captured.update(
            argv=argv, input_bytes=input_bytes, timeout=timeout, max_output=max_output
        )
        request = json.loads(input_bytes)
        response = {
            "response_version": 1,
            "ok": True,
            "context": request["context"],
            "os_release": 'ID=debian\nVERSION_ID="12"\n',
            "simulation": {"returncode": 0, "stdout": ZERO_SIMULATION},
            "reboot_required": None,
        }
        return BoundedProcessResult(0, json.dumps(response).encode(), b"")

    client = SshPackageScanHostControl(
        host="192.0.2.10",
        port=22,
        user="hubinet-scan",
        private_key_path=tmp_path.resolve() / "id_ed25519",
        known_hosts_path=tmp_path.resolve() / "known_hosts",
        timeout_seconds=900,
        max_result_bytes=8 * 1024 * 1024,
        runner=runner,
    )
    result = client.scan_packages(run)
    assert result.simulation_stdout == ZERO_SIMULATION
    argv = captured["argv"]
    assert "StrictHostKeyChecking=yes" in argv
    assert any(str(item).startswith("UserKnownHostsFile=") for item in argv)
    assert "ClearAllForwardings=yes" in argv
    assert argv[-1] == "hubinet-scan@192.0.2.10"
    assert json.loads(captured["input_bytes"])["operation"] == "scan_packages"


@pytest.mark.parametrize(
    "result, expected",
    (
        (BoundedProcessResult(-9, b"", b"", timed_out=True), TimeoutError),
        (
            BoundedProcessResult(-9, b"", b"", output_exceeded=True),
            HostScanFailure,
        ),
        (BoundedProcessResult(255, b"", b"ssh failed"), HostScanFailure),
    ),
)
def test_ssh_host_control_classifies_transport_bounds(
    tmp_path: Path, result: BoundedProcessResult, expected: type[Exception]
) -> None:
    client = SshPackageScanHostControl(
        host="pve-a",
        port=22,
        user="hubinet-scan",
        private_key_path=tmp_path.resolve() / "id_ed25519",
        known_hosts_path=tmp_path.resolve() / "known_hosts",
        timeout_seconds=900,
        max_result_bytes=8 * 1024 * 1024,
        runner=lambda *_args: result,
    )
    with pytest.raises(expected):
        client.scan_packages(_scan_run())
