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
    "text",
    (
        "Inst apt (2.6.2 Debian:12/stable [amd64])\n1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n",
        "Inst apt [2.6.1] broken\n1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n",
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


def _request(*, vmid=101, operation="scan_packages"):
    return {
        "request_version": 1,
        "operation": operation,
        "target": {"vmid": vmid, "expected_node": "pve-a"},
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
        os_release: str = 'ID=debian\nVERSION_ID="12"\n',
        update_returncode: int = 0,
        update_stderr: str = "",
        simulation_returncode: int = 0,
        simulation_stderr: str = "",
        timed_out_command: str | None = None,
    ) -> None:
        self.resource_type = resource_type
        self.status = status
        self.node = node
        self.os_release = os_release
        self.update_returncode = update_returncode
        self.update_stderr = update_stderr
        self.simulation_returncode = simulation_returncode
        self.simulation_stderr = simulation_stderr
        self.timed_out_command = timed_out_command
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv, _timeout, _max_output):
        self.calls.append(argv)
        rendered = " ".join(argv)
        if self.timed_out_command and self.timed_out_command in rendered:
            return helper.CommandResult(-9, b"", b"", timed_out=True)
        if argv[0] == "pvesh":
            rows = [
                {
                    "vmid": 101,
                    "type": self.resource_type,
                    "node": self.node,
                    "status": self.status,
                }
            ]
            return helper.CommandResult(0, json.dumps(rows).encode(), b"")
        if argv[-1] == "/etc/os-release":
            return helper.CommandResult(0, self.os_release.encode(), b"")
        if "update" in argv:
            return helper.CommandResult(
                self.update_returncode, b"", self.update_stderr.encode()
            )
        if "upgrade" in argv:
            return helper.CommandResult(
                self.simulation_returncode,
                ZERO_SIMULATION.encode(),
                self.simulation_stderr.encode(),
            )
        if "/var/run/reboot-required" in argv:
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
    assert any(call[-3:] == ("apt-get", "update", "-qq") for call in runner.calls)
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
