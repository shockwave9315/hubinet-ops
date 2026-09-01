"""The dark PVE package-mutation boundary: journal, lease, and refusals.

This is the only file in the product that can change a workload package, so
its own contract is tested directly rather than only through the
orchestrator: the exact fixed command it runs, the durable at-most-once
journal, the per-VMID lease, and every way it must fail closed.

No real `apt`, `pct`, `ssh`, or PVE command is ever executed. Every host
command is answered by a fake runner, the journal lives in a temporary
directory, and the one test that exercises the real double-fork runs that
same fake runner in the forked child.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[1]
HELPER_PATH = ROOT / "deploy" / "hubinet-package-mutation-helper.py"


def _load_helper() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "hubinet_package_mutation_helper_direct", HELPER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


helper = _load_helper()

NODE = "pve-a"
VMID = 112
BACKEND_ID = "11111111-1111-4111-8111-111111111111"
JOB_ID = "22222222-2222-4222-8222-222222222222"
RESOURCE_ID = "33333333-3333-4333-8333-333333333333"
BINDING_ID = "44444444-4444-4444-8444-444444444444"
OPERATION_ID = "55555555-5555-4555-8555-555555555555"

PACKAGES = [
    {
        "package_name": "apt",
        "architecture": "amd64",
        "installed_version": "2.6.1",
        "candidate_version": "2.6.2",
    },
    {
        "package_name": "zlib1g",
        "architecture": "amd64",
        "installed_version": "1.0",
        "candidate_version": "1.1",
    },
]

OS_RELEASE = 'ID=debian\nVERSION_ID="12"\n'
APT_VERSION = "apt 2.6.1 (amd64)\n"
SIMULATION = (
    "Inst apt [2.6.1] (2.6.2 Debian:12/stable-security [amd64])\n"
    "Inst zlib1g [1.0] (1.1 Debian:12/stable-security [amd64])\n"
    "2 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n"
)


def _request(operation: str, **overrides) -> dict:
    payload = {
        "request_version": 1,
        "operation": operation,
        "target": {"vmid": VMID, "expected_node": NODE},
        "context": {
            "backend_instance_id": BACKEND_ID,
            "job_id": JOB_ID,
            "resource_id": RESOURCE_ID,
            "binding_id": BINDING_ID,
            "locator_generation": 1,
            "resource_continuity_revision": 1,
        },
        "operation_identity": {
            "mutation_operation_id": OPERATION_ID,
            "plan_fingerprint": helper.plan_fingerprint(PACKAGES),
        },
        "expected_packages": [dict(package) for package in PACKAGES],
        "prepared_evidence_digest": None,
    }
    payload.update(overrides)
    return payload


class GuestFilesystem:
    """A minimal in-memory model of the guest paths the helper stages into.

    Only the fixed argv shapes `stage_action_set_gate` issues are answered,
    and the payload arrives exactly the way the real boundary delivers it --
    as stdin bytes, never as command text.
    """

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.modes: dict[str, str] = {}
        self.removed_roots: list[str] = []
        self.readonly = False

    def handle(self, tail, stdin):
        command = tail[2] if len(tail) > 2 else ""
        if command == "rm":
            root = tail[-1]
            self.removed_roots.append(root)
            for path in [p for p in self.files if p.startswith(root)]:
                del self.files[path]
                self.modes.pop(path, None)
            return helper.CommandResult(0, b"", b"")
        if command == "mkdir":
            return helper.CommandResult(0, b"", b"")
        if command == "dd":
            if self.readonly:
                return helper.CommandResult(1, b"", b"Read-only file system\n")
            assert tail[3].startswith("of="), tail
            self.files[tail[3][3:]] = stdin
            return helper.CommandResult(0, b"", b"")
        if command == "chmod":
            self.modes[tail[-1]] = tail[3]
            return helper.CommandResult(0, b"", b"")
        if command == "sha256sum":
            lines = []
            for path in tail[3:]:
                if path not in self.files:
                    return helper.CommandResult(1, b"", b"No such file\n")
                digest = hashlib.sha256(self.files[path]).hexdigest()
                lines.append(f"{digest}  {path}\n")
            return helper.CommandResult(0, "".join(lines).encode(), b"")
        return None


class FakeHost:
    """Answers exactly the fixed argv shapes the helper issues."""

    def __init__(self) -> None:
        self.guest = GuestFilesystem()
        self.installed = {
            ("apt", "amd64"): "2.6.1",
            ("zlib1g", "amd64"): "1.0",
            ("bash", "amd64"): "5.2",
        }
        self.unfinished: list[tuple[str, str, str]] = []
        self.mutations = 0
        self.mutation_exit_code = 0
        self.commands: list[tuple[str, ...]] = []

    def inventory(self) -> str:
        # dpkg reports each (name, architecture) exactly once, in exactly one
        # state, so a package that is mid-transaction is not also listed as
        # installed.
        mid_transaction = {(name, arch) for name, arch, _ in self.unfinished}
        rows = [
            f"{name}\t{architecture}\t{version}\tinstalled\n"
            for (name, architecture), version in sorted(self.installed.items())
            if (name, architecture) not in mid_transaction
        ]
        rows += [
            f"{name}\t{architecture}\t"
            f"{self.installed.get((name, architecture), '0')}\t{status}\n"
            for name, architecture, status in sorted(self.unfinished)
        ]
        return "".join(rows)

    def __call__(self, argv, timeout, max_output, stdin=b""):
        argv = tuple(argv)
        self.commands.append(argv)
        if argv[2:3] == ("/cluster/status",):
            return helper.CommandResult(
                0,
                json.dumps([{"type": "node", "name": NODE, "local": 1}]).encode(),
                b"",
            )
        if argv[2:3] == ("/cluster/resources",):
            return helper.CommandResult(
                0,
                json.dumps(
                    [
                        {
                            "vmid": VMID,
                            "type": "lxc",
                            "node": NODE,
                            "status": "running",
                        }
                    ]
                ).encode(),
                b"",
            )
        tail = argv[4:]
        staged = self.guest.handle(tail, stdin)
        if staged is not None:
            return staged
        if tail[-1] == "/etc/os-release":
            return helper.CommandResult(0, OS_RELEASE.encode(), b"")
        if tail[-1] == "--version":
            return helper.CommandResult(0, APT_VERSION.encode(), b"")
        if tail[-1] == "--print-architecture":
            return helper.CommandResult(0, b"amd64\n", b"")
        if tail[2] == "dpkg-query":
            return helper.CommandResult(0, self.inventory().encode(), b"")
        if "update" in tail:
            return helper.CommandResult(0, b"", b"")
        if "-s" in tail:
            return helper.CommandResult(0, SIMULATION.encode(), b"")
        if tail[-1] == "upgrade":
            assert tuple(tail) == helper.mutation_argv(OPERATION_ID)
            # The real gate runs INSIDE the guest, so a fake that let the
            # mutation proceed without one would be testing a fiction.
            assert helper.guest_verifier_path(OPERATION_ID) in self.guest.files
            assert helper.guest_manifest_path(OPERATION_ID) in self.guest.files
            self.mutations += 1
            if self.mutation_exit_code == 0:
                for package in PACKAGES:
                    self.installed[
                        (package["package_name"], package["architecture"])
                    ] = package["candidate_version"]
            return helper.CommandResult(self.mutation_exit_code, b"done\n", b"")
        raise AssertionError(f"unexpected command: {argv}")


@pytest.fixture()
def journal(tmp_path: Path):
    return helper.OperationJournal(tmp_path / "operations")


@pytest.fixture()
def host():
    return FakeHost()


def _handle(payload, host, journal, *, spawn=None):
    original = helper._spawn_detached_runner
    helper._spawn_detached_runner = spawn if spawn is not None else _synchronous_spawn(
        host
    )
    try:
        return helper.handle_request(payload, runner=host, journal=journal)
    finally:
        helper._spawn_detached_runner = original


def _synchronous_spawn(host):
    def _spawn(
        request,
        journal,
        lease,
        *,
        local_node,
        pre_native_architecture,
        pre_installed_inventory,
        runner,
    ):
        duplicate = os.dup(lease.descriptor)
        lease.detach()
        try:
            helper.run_mutation(
                request,
                journal,
                local_node=local_node,
                pre_native_architecture=pre_native_architecture,
                pre_installed_inventory=pre_installed_inventory,
                runner=runner,
            )
        finally:
            os.close(duplicate)

    return _spawn


def _never_spawn(*_args, **_kwargs):
    raise AssertionError("the helper attempted to launch a package mutation")


def _prepare_then(payload_operation, host, journal, **overrides):
    prepared = _handle(_request("prepare_exact_package_mutation"), host, journal)
    assert prepared["ok"] is True
    digest = prepared["evidence"]["prepared_evidence_digest"]
    return prepared, _handle(
        _request(
            payload_operation, prepared_evidence_digest=digest, **overrides
        ),
        host,
        journal,
    )


def _inspect(host, journal):
    """Read back the durable outcome, exactly the way the backend does.

    `execute_exact_package_mutation` deliberately returns the instant the
    host has journaled `submitted` and detached its runner -- it never waits
    for the package command -- so the terminal result is always observed
    through a later read-only inspection.
    """

    return _handle(
        _request("inspect_package_mutation_state"), host, journal, spawn=_never_spawn
    )


# ===========================================================================
# A. THE ONE REAL PACKAGE COMMAND
# ===========================================================================


def test_the_mutation_command_is_fixed_bounded_and_carries_no_package_name() -> None:
    argv = helper.mutation_argv(OPERATION_ID)
    assert argv[0] == "env"
    assert "DEBIAN_FRONTEND=noninteractive" in argv
    assert argv[-1] == "upgrade"
    assert "-y" in argv
    # Upgrade semantics only. Never a broader apt sub-command.
    for forbidden in ("install", "dist-upgrade", "full-upgrade", "remove", "purge",
                      "autoremove", "--force-yes"):
        assert forbidden not in argv, forbidden
    # Deterministic conffile policy: the operator's file is preserved and the
    # distributor's is left as .dpkg-dist. Without this dpkg aborts
    # mid-transaction on end-of-file at its conffile prompt.
    assert "Dpkg::Options::=--force-confold" in argv
    assert "Dpkg::Options::=--force-confdef" in argv
    # Belt-and-braces against a guest apt.conf.d snippet flipping a default.
    for option in (
        "APT::Get::Upgrade-Allow-New=false",
        "APT::Get::Remove=false",
        "APT::Get::allow-downgrades=false",
        "APT::Get::allow-remove-essential=false",
        "APT::Get::allow-change-held-packages=false",
        "APT::Get::AllowUnauthenticated=false",
        "APT::Ignore-Hold=false",
    ):
        assert option in argv, option
    # No package name, version, or caller value can ever appear in it.
    for package in PACKAGES:
        assert package["package_name"] not in argv
        assert package["candidate_version"] not in argv
    # Not merely absent as whole words: no version string may appear
    # anywhere inside any argument either. (Package NAMES are not checked
    # this way on purpose -- one of the fixture packages is literally named
    # `apt`, which legitimately occurs inside `apt-get`.)
    for word in argv:
        for package in PACKAGES:
            assert package["installed_version"] not in word, word
            assert package["candidate_version"] not in word, word


def test_the_mutation_command_installs_its_own_version_3_action_gate() -> None:
    """The real invocation carries the pre-dpkg gate, keyed the only way
    APT can actually resolve it."""

    argv = helper.mutation_argv(OPERATION_ID)
    verifier = helper.guest_verifier_path(OPERATION_ID)

    assert f"DPkg::Pre-Install-Pkgs::={verifier}" in argv
    assert f"DPkg::Tools::Options::{verifier}::Version=3" in argv

    # APT keys the per-command version option on the EXACT hook command
    # string. A command containing whitespace does not resolve its own
    # option and silently falls back to protocol Version 1, so the hook
    # command must stay one bare path.
    assert " " not in verifier
    assert verifier.startswith(helper.GUEST_STAGING_ROOT + "/")
    assert verifier.endswith("/" + helper.GUEST_VERIFIER_NAME)

    # It is a fixed, code-owned command: no request text, no option, and no
    # shell fragment can enter it.
    assert OPERATION_ID in verifier
    for package in PACKAGES:
        assert package["package_name"] not in verifier
        assert package["candidate_version"] not in verifier
    for metacharacter in (";", "|", "&", "$", "`", "(", ")", "<", ">", "'", '"', "\\"):
        assert metacharacter not in verifier, metacharacter


def test_the_action_gate_path_is_derived_only_from_a_canonical_uuid() -> None:
    """A caller cannot steer the staged path, so it can never become a
    generic write-anywhere primitive."""

    for hostile in (
        "../../etc/cron.d/x",
        "55555555-5555-4555-8555-555555555555/../..",
        "not-a-uuid",
        "55555555555545558555555555555555",
        "",
    ):
        with pytest.raises((ValueError, helper.RequestError)):
            helper.guest_verifier_path(hostile)


def test_a_prepared_mutation_runs_exactly_one_package_command(
    host, journal
) -> None:
    _, submitted = _prepare_then("execute_exact_package_mutation", host, journal)
    assert submitted["ok"] is True
    assert submitted["operation_state"] == "submitted"

    executed = _inspect(host, journal)

    assert executed["operation_state"] == "terminal_success"
    assert host.mutations == 1
    evidence = executed["evidence"]
    assert evidence["exit_code"] == 0
    assert evidence["timed_out"] is False
    assert "apt\tamd64\t2.6.1\tinstalled" in evidence["pre_installed_inventory"]
    assert "apt\tamd64\t2.6.2\tinstalled" in evidence["post_installed_inventory"]


def test_preparation_alone_never_mutates_anything(host, journal) -> None:
    prepared = _handle(
        _request("prepare_exact_package_mutation"), host, journal, spawn=_never_spawn
    )

    assert prepared["ok"] is True
    assert prepared["operation_state"] == "intent"
    assert host.mutations == 0
    assert journal.read(OPERATION_ID)["phase"] == "intent"


# ===========================================================================
# B. AT-MOST-ONCE
# ===========================================================================


def test_a_repeated_execute_never_runs_a_second_package_command(
    host, journal
) -> None:
    prepared, _ = _prepare_then("execute_exact_package_mutation", host, journal)
    digest = prepared["evidence"]["prepared_evidence_digest"]
    first = _inspect(host, journal)

    second = _handle(
        _request("execute_exact_package_mutation", prepared_evidence_digest=digest),
        host,
        journal,
        spawn=_never_spawn,
    )

    assert second["ok"] is True
    assert second["operation_state"] == "terminal_success"
    assert second["evidence"] == first["evidence"]
    assert host.mutations == 1


def test_execute_from_a_submitted_journal_never_resubmits(host, journal) -> None:
    _handle(_request("prepare_exact_package_mutation"), host, journal)
    record = journal.read(OPERATION_ID)
    journal.write(
        {
            key: value
            for key, value in {**record, "phase": "submitted"}.items()
            if key != "prepared_evidence_digest"
        }
    )

    response = _handle(
        _request(
            "execute_exact_package_mutation",
            prepared_evidence_digest=helper.evidence_digest(
                {
                    "os_release": OS_RELEASE,
                    "native_architecture": "amd64\n",
                    "installed_inventory": host.inventory(),
                    "simulation_stdout": SIMULATION,
                }
            ),
        ),
        host,
        journal,
        spawn=_never_spawn,
    )

    assert response["ok"] is True
    assert response["operation_state"] == "submitted"
    assert host.mutations == 0


def test_execute_without_a_prepare_refuses(host, journal) -> None:
    response = _handle(
        _request(
            "execute_exact_package_mutation", prepared_evidence_digest="a" * 64
        ),
        host,
        journal,
        spawn=_never_spawn,
    )

    assert response["ok"] is False
    assert response["error"]["classification"] == "mutation_state_mismatch"
    assert host.mutations == 0


def test_execute_with_the_wrong_prepared_evidence_refuses(host, journal) -> None:
    _handle(_request("prepare_exact_package_mutation"), host, journal)

    response = _handle(
        _request(
            "execute_exact_package_mutation", prepared_evidence_digest="b" * 64
        ),
        host,
        journal,
        spawn=_never_spawn,
    )

    assert response["ok"] is False
    assert response["error"]["classification"] == "mutation_state_mismatch"
    assert host.mutations == 0


# ===========================================================================
# C. THE DURABLE SEAL
# ===========================================================================


@pytest.mark.parametrize("prepare_first", [False, True])
def test_a_seal_durably_forbids_a_future_mutation(
    host, journal, prepare_first
) -> None:
    if prepare_first:
        _handle(_request("prepare_exact_package_mutation"), host, journal)
    sealed = _handle(
        _request("seal_mutation_never_submitted"), host, journal, spawn=_never_spawn
    )

    assert sealed["ok"] is True
    assert sealed["operation_state"] == "sealed_not_submitted"
    assert journal.read(OPERATION_ID)["phase"] == "sealed_not_submitted"

    # Every later attempt -- including a delayed one carrying a valid
    # prepared digest -- must obey the seal.
    late = _handle(
        _request(
            "execute_exact_package_mutation", prepared_evidence_digest="c" * 64
        ),
        host,
        journal,
        spawn=_never_spawn,
    )
    assert late["operation_state"] == "sealed_not_submitted"
    assert host.mutations == 0

    again = _handle(
        _request("seal_mutation_never_submitted"), host, journal, spawn=_never_spawn
    )
    assert again["operation_state"] == "sealed_not_submitted"


def test_a_seal_after_submission_refuses_and_reports_the_real_phase(
    host, journal
) -> None:
    _prepare_then("execute_exact_package_mutation", host, journal)

    sealed = _handle(
        _request("seal_mutation_never_submitted"), host, journal, spawn=_never_spawn
    )

    assert sealed["operation_state"] == "terminal_success"
    assert journal.read(OPERATION_ID)["phase"] == "terminal_success"


def test_a_seal_performs_no_pve_or_guest_reads(host, journal) -> None:
    """A moved or unreachable guest must never block the release proof."""

    _handle(_request("prepare_exact_package_mutation"), host, journal)
    host.commands.clear()

    sealed = _handle(
        _request("seal_mutation_never_submitted"), host, journal, spawn=_never_spawn
    )

    assert sealed["operation_state"] == "sealed_not_submitted"
    assert host.commands == []


def test_inspection_performs_no_pve_or_guest_reads(host, journal) -> None:
    _handle(_request("prepare_exact_package_mutation"), host, journal)
    host.commands.clear()

    inspected = _handle(
        _request("inspect_package_mutation_state"), host, journal, spawn=_never_spawn
    )

    assert inspected["operation_state"] == "intent"
    assert host.commands == []


# ===========================================================================
# D. THE PER-VMID LEASE
# ===========================================================================


def test_a_held_lease_refuses_every_mutating_operation(host, journal) -> None:
    with helper.VmidMutationLease(VMID, journal.directory):
        for operation in (
            "prepare_exact_package_mutation",
            "execute_exact_package_mutation",
            "seal_mutation_never_submitted",
        ):
            payload = _request(
                operation,
                prepared_evidence_digest=(
                    "d" * 64
                    if operation == "execute_exact_package_mutation"
                    else None
                ),
            )
            response = _handle(payload, host, journal, spawn=_never_spawn)
            assert response["ok"] is False, operation
            assert response["error"]["classification"] == "operation_in_progress"
    assert host.mutations == 0


def test_a_held_lease_is_the_running_signal_for_a_submitted_operation(
    host, journal
) -> None:
    _handle(_request("prepare_exact_package_mutation"), host, journal)
    record = journal.read(OPERATION_ID)
    journal.write(
        {
            key: value
            for key, value in {**record, "phase": "submitted"}.items()
            if key != "prepared_evidence_digest"
        }
    )

    idle = _handle(
        _request("inspect_package_mutation_state"), host, journal, spawn=_never_spawn
    )
    assert idle["operation_state"] == "submitted"
    assert idle["running"] is False

    with helper.VmidMutationLease(VMID, journal.directory):
        busy = _handle(
            _request("inspect_package_mutation_state"),
            host,
            journal,
            spawn=_never_spawn,
        )
    assert busy["operation_state"] == "submitted"
    assert busy["running"] is True


def test_the_real_detached_runner_holds_the_lease_and_journals_its_result(
    host, journal, tmp_path: Path
) -> None:
    """The real double-fork path, with a fake runner in the forked child.

    Proves the properties the mutation's durable fate depends on: the runner
    keeps the per-VMID lease across the handover (so "a mutation is running"
    is observable), it is reparented away from the caller, and it journals
    exactly one terminal record.
    """

    _handle(_request("prepare_exact_package_mutation"), host, journal)
    digest = journal.read(OPERATION_ID)["prepared_evidence_digest"]

    marker = tmp_path / "runner-ran"

    def _fake_runner(argv, timeout, max_output, stdin=b""):
        if tuple(argv[4:])[-1] == "upgrade" and "-s" not in argv:
            marker.write_text(str(os.getpid()), encoding="utf-8")
        return host(argv, timeout, max_output, stdin)

    response = helper.handle_request(
        _request(
            "execute_exact_package_mutation", prepared_evidence_digest=digest
        ),
        runner=_fake_runner,
        journal=journal,
    )

    assert response["ok"] is True
    assert response["operation_state"] == "submitted"
    assert response["running"] is True

    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        record = journal.read(OPERATION_ID)
        if record["phase"] in helper.TERMINAL_PHASES:
            break
        time.sleep(0.05)
    record = journal.read(OPERATION_ID)
    assert record["phase"] == "terminal_success", record["phase"]
    assert record["result"]["exit_code"] == 0
    # The command really ran in a DIFFERENT process from this test.
    assert marker.exists()
    assert marker.read_text(encoding="utf-8") != str(os.getpid())
    # And the lease is released once the runner process actually exits. The
    # terminal record is journaled just before that, so this is a bounded
    # wait, not an instantaneous assertion.
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if not helper.lease_is_held(VMID, journal.directory):
            break
        time.sleep(0.05)
    assert not helper.lease_is_held(VMID, journal.directory)


# ===========================================================================
# E. LAST-INSTANT REFUSALS
# ===========================================================================


def test_installed_state_drift_refuses_before_any_mutation(host, journal) -> None:
    prepared = _handle(_request("prepare_exact_package_mutation"), host, journal)
    digest = prepared["evidence"]["prepared_evidence_digest"]
    # Somebody else upgraded an approved package in the meantime.
    host.installed[("apt", "amd64")] = "2.6.5"

    response = _handle(
        _request("execute_exact_package_mutation", prepared_evidence_digest=digest),
        host,
        journal,
        spawn=_never_spawn,
    )

    assert response["ok"] is False
    assert response["error"]["classification"] == "mutation_state_mismatch"
    assert host.mutations == 0
    assert journal.read(OPERATION_ID)["phase"] == "intent"


def test_unfinished_dpkg_state_refuses_before_any_mutation(host, journal) -> None:
    prepared = _handle(_request("prepare_exact_package_mutation"), host, journal)
    digest = prepared["evidence"]["prepared_evidence_digest"]
    host.unfinished.append(("bash", "amd64", "half-configured"))

    response = _handle(
        _request("execute_exact_package_mutation", prepared_evidence_digest=digest),
        host,
        journal,
        spawn=_never_spawn,
    )

    assert response["ok"] is False
    assert "unfinished dpkg" in response["error"]["message"]
    assert host.mutations == 0


@pytest.mark.parametrize(
    ("row", "classification"),
    [
        ({"vmid": VMID, "type": "qemu", "node": NODE, "status": "running"},
         "unsupported_resource_type"),
        ({"vmid": VMID, "type": "lxc", "node": "pve-b", "status": "running"},
         "stale_target"),
        ({"vmid": VMID, "type": "lxc", "node": NODE, "status": "stopped"},
         "guest_unavailable"),
    ],
)
def test_live_target_revalidation_refuses_before_any_mutation(
    host, journal, row, classification
) -> None:
    prepared = _handle(_request("prepare_exact_package_mutation"), host, journal)
    digest = prepared["evidence"]["prepared_evidence_digest"]

    original = host.__call__

    def _moved(argv, timeout, max_output, stdin=b""):
        if tuple(argv)[2:3] == ("/cluster/resources",):
            return helper.CommandResult(0, json.dumps([row]).encode(), b"")
        return original(argv, timeout, max_output, stdin)

    response = _handle(
        _request("execute_exact_package_mutation", prepared_evidence_digest=digest),
        _moved,
        journal,
        spawn=_never_spawn,
    )

    assert response["ok"] is False
    assert response["error"]["classification"] == classification
    assert host.mutations == 0


def test_a_failed_package_command_still_journals_its_post_state(
    host, journal
) -> None:
    host.mutation_exit_code = 100
    _prepare_then("execute_exact_package_mutation", host, journal)

    executed = _inspect(host, journal)

    assert executed["operation_state"] == "terminal_failure"
    assert executed["evidence"]["exit_code"] == 100
    # The post-state is captured even on failure: a failed mutation may still
    # have changed packages, and that evidence is what rollback needs.
    assert executed["evidence"]["post_installed_inventory"]
    assert host.mutations == 1


# ===========================================================================
# F. JOURNAL CORRUPTION AND REQUEST FENCING
# ===========================================================================


@pytest.mark.parametrize(
    "record",
    [
        {"journal_version": 2, "mutation_operation_id": OPERATION_ID,
         "phase": "intent", "request_fingerprint": "x", "vmid": VMID},
        {"journal_version": 1, "mutation_operation_id": OPERATION_ID,
         "phase": "teleported", "request_fingerprint": "x", "vmid": VMID},
        {"journal_version": 1, "mutation_operation_id": OPERATION_ID,
         "phase": "intent", "request_fingerprint": "x", "vmid": "112"},
        # A pre-submission phase carrying mutation result evidence.
        {"journal_version": 1, "mutation_operation_id": OPERATION_ID,
         "phase": "intent", "request_fingerprint": "x", "vmid": VMID,
         "prepared_evidence_digest": "a" * 64, "result": {"exit_code": 0}},
        # A terminal phase with no result evidence at all.
        {"journal_version": 1, "mutation_operation_id": OPERATION_ID,
         "phase": "terminal_success", "request_fingerprint": "x", "vmid": VMID},
        # `intent` with no prepared-evidence binding.
        {"journal_version": 1, "mutation_operation_id": OPERATION_ID,
         "phase": "intent", "request_fingerprint": "x", "vmid": VMID},
        # A seal that still carries a prepared-evidence binding.
        {"journal_version": 1, "mutation_operation_id": OPERATION_ID,
         "phase": "sealed_not_submitted", "request_fingerprint": "x",
         "vmid": VMID, "prepared_evidence_digest": "a" * 64},
    ],
)
def test_a_corrupt_journal_fails_closed_and_never_mutates(
    host, journal, record
) -> None:
    journal.ensure_directory()
    (journal.directory / f"op-{OPERATION_ID}.json").write_text(
        json.dumps(record), encoding="utf-8"
    )

    for operation in (
        "inspect_package_mutation_state",
        "seal_mutation_never_submitted",
        "prepare_exact_package_mutation",
        "execute_exact_package_mutation",
    ):
        response = _handle(
            _request(
                operation,
                prepared_evidence_digest=(
                    "a" * 64
                    if operation == "execute_exact_package_mutation"
                    else None
                ),
            ),
            host,
            journal,
            spawn=_never_spawn,
        )
        assert response["ok"] is False, operation
        assert response["error"]["classification"] == "journal_corrupt", operation
    assert host.mutations == 0


def test_a_terminal_phase_contradicting_its_own_result_fails_closed(
    host, journal
) -> None:
    journal.ensure_directory()
    (journal.directory / f"op-{OPERATION_ID}.json").write_text(
        json.dumps(
            {
                "journal_version": 1,
                "mutation_operation_id": OPERATION_ID,
                "phase": "terminal_success",
                "request_fingerprint": "x",
                "vmid": VMID,
                "result": {
                    "exit_code": 100,
                    "timed_out": False,
                    "pre_installed_inventory": "a",
                    "post_installed_inventory": "b",
                    "post_native_architecture": "amd64",
                },
            }
        ),
        encoding="utf-8",
    )

    response = _handle(
        _request("inspect_package_mutation_state"), host, journal, spawn=_never_spawn
    )

    assert response["ok"] is False
    assert response["error"]["classification"] == "journal_corrupt"


def test_malformed_journal_bytes_fail_closed(host, journal) -> None:
    journal.ensure_directory()
    (journal.directory / f"op-{OPERATION_ID}.json").write_bytes(b"{not json")

    response = _handle(
        _request("inspect_package_mutation_state"), host, journal, spawn=_never_spawn
    )

    assert response["ok"] is False
    assert response["error"]["classification"] == "journal_corrupt"


@pytest.mark.parametrize(
    "override",
    [
        {"target": {"vmid": 999, "expected_node": NODE}},
        {"target": {"vmid": VMID, "expected_node": "pve-b"}},
        {
            "context": {
                "backend_instance_id": OPERATION_ID,
                "job_id": JOB_ID,
                "resource_id": RESOURCE_ID,
                "binding_id": BINDING_ID,
                "locator_generation": 1,
                "resource_continuity_revision": 1,
            }
        },
        {
            "context": {
                "backend_instance_id": BACKEND_ID,
                "job_id": JOB_ID,
                "resource_id": RESOURCE_ID,
                "binding_id": BINDING_ID,
                "locator_generation": 1,
                "resource_continuity_revision": 2,
            }
        },
    ],
)
def test_a_different_request_can_never_reuse_an_operation_identity(
    host, journal, override
) -> None:
    _handle(_request("prepare_exact_package_mutation"), host, journal)

    payload = _request("inspect_package_mutation_state", **override)
    response = _handle(payload, host, journal, spawn=_never_spawn)

    assert response["ok"] is False
    assert response["error"]["classification"] == "operation_request_mismatch"
    assert host.mutations == 0


def test_a_plan_fingerprint_that_does_not_describe_its_packages_is_refused(
    host, journal
) -> None:
    payload = _request("prepare_exact_package_mutation")
    payload["expected_packages"][0]["candidate_version"] = "9.9"

    with pytest.raises(helper.RequestError, match="plan_fingerprint"):
        helper.validate_request(payload)


# ===========================================================================
# G. REQUEST VALIDATION
# ===========================================================================


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.__setitem__("operation", "run_anything"),
        lambda p: p.__setitem__("request_version", 2),
        lambda p: p.pop("expected_packages"),
        lambda p: p.__setitem__("expected_packages", []),
        lambda p: p["target"].__setitem__("vmid", 0),
        lambda p: p["target"].__setitem__("expected_node", "-oProxyCommand=x"),
        lambda p: p["operation_identity"].__setitem__(
            "mutation_operation_id", "not-a-uuid"
        ),
        lambda p: p["operation_identity"].__setitem__("plan_fingerprint", "short"),
        lambda p: p["context"].__setitem__("locator_generation", 0),
        lambda p: p.__setitem__("prepared_evidence_digest", "x"),
        lambda p: p.__setitem__("extra", 1),
    ],
)
def test_a_malformed_request_is_refused(mutate) -> None:
    payload = _request("prepare_exact_package_mutation")
    mutate(payload)
    with pytest.raises(helper.RequestError):
        helper.validate_request(payload)


def test_a_duplicate_package_identity_is_refused() -> None:
    payload = _request("prepare_exact_package_mutation")
    payload["expected_packages"] = [dict(PACKAGES[0]), dict(PACKAGES[0])]
    payload["operation_identity"]["plan_fingerprint"] = helper.plan_fingerprint(
        payload["expected_packages"]
    )
    with pytest.raises(helper.RequestError, match="duplicate"):
        helper.validate_request(payload)


def test_a_prepared_digest_on_a_non_executing_operation_is_refused() -> None:
    payload = _request(
        "prepare_exact_package_mutation", prepared_evidence_digest="a" * 64
    )
    with pytest.raises(helper.RequestError, match="only meaningful"):
        helper.validate_request(payload)


def test_remote_command_text_is_never_accepted(monkeypatch) -> None:
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "rm -rf /")
    assert helper.main() == 2


def test_the_helper_fingerprint_matches_the_backends(host, journal) -> None:
    from app.inventory import PackageScanPackage
    from app.inventory.authority import package_plan_fingerprint

    backend = package_plan_fingerprint(
        tuple(
            PackageScanPackage(
                package_name=package["package_name"],
                architecture=package["architecture"],
                installed_version=package["installed_version"],
                candidate_version=package["candidate_version"],
            )
            for package in PACKAGES
        )
    )
    assert helper.plan_fingerprint(PACKAGES) == backend


# ===========================================================================
# J. THE PRE-DPKG ACTION GATE
#
# The real `apt-get upgrade` resolves against APT state that can legitimately
# have changed since PREPARE simulated it -- a completed `apt-get update`, a
# released hold, a new pin or source -- while every installed version still
# matches the approved plan. So the invocation's OWN resolved action stream
# is compared to the authority-accepted material before dpkg is reached.
#
# These tests run the REAL generated verifier against REAL protocol Version 3
# records. Nothing here executes apt, dpkg, pct, or ssh: the verifier is a
# pure stdin-to-exit-code text gate, and running it is the only way to test
# what it actually does rather than restate it.
# ===========================================================================


V3_CONFIG_SECTION = (
    "VERSION 3\n"
    "APT::Architecture=amd64\n"
    "APT::Get::Assume-Yes=1\n"
    "Dir::Bin::dpkg=/usr/bin/dpkg\n"
    "DPkg::Pre-Install-Pkgs::=/usr/bin/apt-listchanges%20--apt\n"
    "\n"
)


def _actions(*rows: str) -> str:
    return "".join(f"{row}\n" for row in rows)


#: The exact stream real APT emits for this fixture's approved plan. Field
#: order and spelling were captured from `apt 3.0.3` itself.
APPROVED_STREAM = _actions(
    "apt 2.6.1 amd64 none < 2.6.2 amd64 none /var/cache/apt/archives/apt_2.6.2_amd64.deb",
    "zlib1g 1.0 amd64 none < 1.1 amd64 none /var/cache/apt/archives/zlib1g_1.1_amd64.deb",
    "apt 2.6.1 amd64 none < 2.6.2 amd64 none **CONFIGURE**",
    "zlib1g 1.0 amd64 none < 1.1 amd64 none **CONFIGURE**",
)


@pytest.fixture()
def gate(tmp_path: Path, monkeypatch):
    """Materialize the real verifier and manifest under a temporary root."""

    monkeypatch.setattr(helper, "GUEST_STAGING_ROOT", str(tmp_path / "run"))
    directory = Path(helper.guest_staging_directory(OPERATION_ID))
    directory.mkdir(parents=True)
    manifest = Path(helper.guest_manifest_path(OPERATION_ID))
    manifest.write_text(
        helper.build_expected_action_manifest(OPERATION_ID, PACKAGES),
        encoding="utf-8",
    )
    verifier = Path(helper.guest_verifier_path(OPERATION_ID))
    verifier.write_text(
        helper.build_action_set_verifier(OPERATION_ID), encoding="utf-8"
    )
    verifier.chmod(0o700)

    def _run(stream: str) -> tuple[int, str]:
        completed = subprocess.run(  # noqa: S603 - a generated text gate, no network
            [str(verifier)],
            input=stream.encode("utf-8"),
            capture_output=True,
            timeout=60,
        )
        return completed.returncode, completed.stderr.decode("utf-8", "replace")

    _run.directory = directory
    _run.manifest = manifest
    _run.verifier = verifier
    return _run


def test_the_gate_accepts_exactly_the_approved_action_stream(gate) -> None:
    """The positive control: legal stays legal."""

    returncode, stderr = gate(V3_CONFIG_SECTION + APPROVED_STREAM)
    assert returncode == 0, stderr
    assert stderr == ""


def test_the_gate_accepts_the_approved_set_in_any_planned_order(gate) -> None:
    """Order is APT's business; the SET is the approved thing.

    A different dependency ordering reaches the identical end state, so the
    comparison is exact multiset equality, not a line-by-line diff against
    the order APT happened to choose.
    """

    reordered = _actions(
        "zlib1g 1.0 amd64 none < 1.1 amd64 none **CONFIGURE**",
        "apt 2.6.1 amd64 none < 2.6.2 amd64 none /var/cache/apt/archives/a.deb",
        "apt 2.6.1 amd64 none < 2.6.2 amd64 none **CONFIGURE**",
        "zlib1g 1.0 amd64 none < 1.1 amd64 none /var/cache/apt/archives/z.deb",
    )
    returncode, stderr = gate(V3_CONFIG_SECTION + reordered)
    assert returncode == 0, stderr


def test_the_gate_accepts_any_documented_multiarch_type(gate) -> None:
    """MultiArch type is validated, not bound.

    APT reports the type OF THE VERSION being acted on, and it legitimately
    differs between a package's installed and candidate versions (observed
    in real APT: `becomesall 2.0 amd64 none < 2.1 all foreign`). Binding it
    would fail-close on legal upgrades while adding nothing: the binary
    identity dpkg acts on is already pinned by name, version, and
    architecture.
    """

    stream = _actions(
        "apt 2.6.1 amd64 none < 2.6.2 amd64 foreign /var/cache/apt/archives/a.deb",
        "apt 2.6.1 amd64 none < 2.6.2 amd64 foreign **CONFIGURE**",
        "zlib1g 1.0 amd64 same < 1.1 amd64 allowed /var/cache/apt/archives/z.deb",
        "zlib1g 1.0 amd64 same < 1.1 amd64 allowed **CONFIGURE**",
    )
    returncode, stderr = gate(V3_CONFIG_SECTION + stream)
    assert returncode == 0, stderr


@pytest.mark.parametrize(
    ("case", "stream"),
    [
        # A. The approved candidate changed after PREPARE: someone else
        #    completed an `apt-get update` and 2.6.3 is now the candidate.
        #    dpkg's installed versions still match the approved plan, so the
        #    installed-version fence alone would let this through.
        (
            "changed_candidate",
            V3_CONFIG_SECTION
            + _actions(
                "apt 2.6.1 amd64 none < 2.6.3 amd64 none /v/apt_2.6.3_amd64.deb",
                "apt 2.6.1 amd64 none < 2.6.3 amd64 none **CONFIGURE**",
                "zlib1g 1.0 amd64 none < 1.1 amd64 none /v/z.deb",
                "zlib1g 1.0 amd64 none < 1.1 amd64 none **CONFIGURE**",
            ),
        ),
        # B. An unrelated installed package became newly upgradable.
        (
            "unrelated_now_upgradable",
            V3_CONFIG_SECTION
            + APPROVED_STREAM
            + _actions(
                "curl 7.88.1 amd64 none < 7.88.2 amd64 none /v/curl.deb",
                "curl 7.88.1 amd64 none < 7.88.2 amd64 none **CONFIGURE**",
            ),
        ),
        # C. A held package became newly eligible.
        (
            "hold_released",
            V3_CONFIG_SECTION
            + APPROVED_STREAM
            + _actions(
                "nginx 1.22 amd64 none < 1.24 amd64 none /v/nginx.deb",
                "nginx 1.22 amd64 none < 1.24 amd64 none **CONFIGURE**",
            ),
        ),
        # D. A pin/preferences/source change dropped an approved package out
        #    of the resolved set.
        (
            "pin_dropped_an_approved_package",
            V3_CONFIG_SECTION
            + _actions(
                "apt 2.6.1 amd64 none < 2.6.2 amd64 none /v/apt.deb",
                "apt 2.6.1 amd64 none < 2.6.2 amd64 none **CONFIGURE**",
            ),
        ),
        # G. New install, removal, downgrade, wrong architecture, wrong
        #    old/new version, extra action.
        (
            "new_install",
            V3_CONFIG_SECTION
            + APPROVED_STREAM
            + _actions(
                "libnew - - none < 1.0 amd64 none /v/libnew.deb",
                "libnew - - none < 1.0 amd64 none **CONFIGURE**",
            ),
        ),
        (
            "removal",
            V3_CONFIG_SECTION
            + APPROVED_STREAM
            + _actions("old 1.0 amd64 none > - - none **REMOVE**"),
        ),
        (
            "downgrade",
            V3_CONFIG_SECTION
            + _actions(
                "apt 2.6.1 amd64 none > 2.6.0 amd64 none /v/apt_2.6.0_amd64.deb",
                "apt 2.6.1 amd64 none > 2.6.0 amd64 none **CONFIGURE**",
                "zlib1g 1.0 amd64 none < 1.1 amd64 none /v/z.deb",
                "zlib1g 1.0 amd64 none < 1.1 amd64 none **CONFIGURE**",
            ),
        ),
        (
            "wrong_architecture",
            V3_CONFIG_SECTION
            + _actions(
                "apt 2.6.1 i386 none < 2.6.2 i386 none /v/apt_2.6.2_i386.deb",
                "apt 2.6.1 i386 none < 2.6.2 i386 none **CONFIGURE**",
                "zlib1g 1.0 amd64 none < 1.1 amd64 none /v/z.deb",
                "zlib1g 1.0 amd64 none < 1.1 amd64 none **CONFIGURE**",
            ),
        ),
        (
            "wrong_old_version",
            V3_CONFIG_SECTION
            + _actions(
                "apt 2.6.0 amd64 none < 2.6.2 amd64 none /v/apt.deb",
                "apt 2.6.0 amd64 none < 2.6.2 amd64 none **CONFIGURE**",
                "zlib1g 1.0 amd64 none < 1.1 amd64 none /v/z.deb",
                "zlib1g 1.0 amd64 none < 1.1 amd64 none **CONFIGURE**",
            ),
        ),
        (
            "duplicate_action",
            V3_CONFIG_SECTION
            + APPROVED_STREAM
            + _actions("apt 2.6.1 amd64 none < 2.6.2 amd64 none **CONFIGURE**"),
        ),
        # An action word this product does not perform must never be read as
        # an unpack merely because it is not one of the two known words.
        (
            "unknown_action_word",
            V3_CONFIG_SECTION
            + _actions("apt 2.6.1 amd64 none < 2.6.2 amd64 none **PURGE**"),
        ),
        (
            "unpack_that_is_not_a_deb",
            V3_CONFIG_SECTION
            + _actions("apt 2.6.1 amd64 none < 2.6.2 amd64 none /tmp/payload"),
        ),
        # F. Protocol. APT silently downgrades rather than failing, so the
        #    marker itself must be proven.
        ("protocol_v2", "VERSION 2\nDir=/\n\n" + APPROVED_STREAM),
        ("protocol_v1", "/var/cache/apt/archives/apt_2.6.2_amd64.deb\n"),
        ("empty_stream", ""),
        ("no_action_section", "VERSION 3\nDir=/\n"),
        ("no_actions_at_all", V3_CONFIG_SECTION),
        # Malformed records.
        (
            "unknown_multiarch_type",
            V3_CONFIG_SECTION
            + _actions("apt 2.6.1 amd64 bogus < 2.6.2 amd64 none /v/apt.deb"),
        ),
        (
            "trailing_field",
            V3_CONFIG_SECTION
            + _actions("apt 2.6.1 amd64 none < 2.6.2 amd64 none **CONFIGURE** extra"),
        ),
        ("truncated_record", _actions("apt 2.6.1 amd64 none < 2.6.2")),
    ],
)
def test_the_gate_refuses_every_divergence_before_dpkg(gate, case, stream) -> None:
    """Each of these aborts APT before dpkg receives one package operation.

    That APT actually aborts on a non-zero hook -- and that its dpkg
    package-operation count is then zero -- is upstream behaviour
    (`RunScriptsWithPkgs` returning false ahead of the dpkg loop in
    `apt-pkg/deb/dpkgpm.cc`), re-verified against a real `apt` in an
    isolated APT root while building this gate.
    """

    returncode, stderr = gate(stream)
    assert returncode != 0, f"{case} was accepted"
    assert "refusing package mutation before dpkg" in stderr, stderr


def test_a_manifest_from_another_operation_can_never_authorize_this_one(
    gate,
) -> None:
    """A stale `/run` artifact is data, never permission.

    The staging root is wiped and recreated under the per-VMID lease before
    every submission, so a leftover should not exist at all. If one somehow
    did, the manifest header and the verifier's own literal both name an
    operation, and they must agree.
    """

    other = "66666666-6666-4666-8666-666666666666"
    gate.manifest.write_text(
        helper.build_expected_action_manifest(other, PACKAGES), encoding="utf-8"
    )

    returncode, stderr = gate(V3_CONFIG_SECTION + APPROVED_STREAM)

    assert returncode != 0
    assert "belongs to another operation" in stderr


def test_a_missing_manifest_refuses_rather_than_waving_the_mutation_through(
    gate,
) -> None:
    gate.manifest.unlink()
    returncode, stderr = gate(V3_CONFIG_SECTION + APPROVED_STREAM)
    assert returncode != 0
    assert "expected action manifest is missing" in stderr


def test_the_gate_runs_on_guaranteed_base_utilities_only() -> None:
    """No new guest prerequisite.

    The verifier is `/bin/sh` plus `sort`, `tail`, and `printf`. `dash` and
    `coreutils` are both `Essential: yes` under Debian Policy, so every
    supported Debian-family guest already has them; nothing here assumes
    Python, Perl, or awk inside the guest.
    """

    script = helper.build_action_set_verifier(OPERATION_ID)

    assert script.startswith("#!/bin/sh\n")
    # Word-boundary matches: "differs" in a refusal message is prose, not a
    # call to diff(1).
    for absent in ("python", "python3", "perl", "awk", "gawk", "mawk", "jq",
                   "diff", "cmp", "grep", "sed", "xargs"):
        assert re.search(rf"\b{absent}\b", script) is None, absent
    # The external commands it does use, both from Essential coreutils.
    assert re.search(r"\bsort\b", script) is not None
    assert re.search(r"\btail\b", script) is not None
    # `<&$VAR` is a syntax error in dash, so the stream is read from stdin
    # (APT's default InfoFD) rather than a variable file descriptor.
    assert "APT_HOOK_INFO_FD" not in script


def test_no_package_value_can_reach_the_gate_command_or_its_script() -> None:
    """Approved material is data in a file, never text in a command.

    The hook command APT runs through a shell is a fixed bare path; the
    package material lives only in a manifest staged as stdin payload bytes.
    So there is no value a package name or version could take that changes
    what executes.
    """

    hostile = [
        {
            "package_name": "evil; rm -rf /",
            "architecture": "amd64",
            "installed_version": "1.0",
            "candidate_version": "1.1",
        }
    ]
    with pytest.raises(helper.MutationError, match="exact action"):
        helper.build_expected_action_manifest(OPERATION_ID, hostile)

    for field, value in (
        ("installed_version", "1.0\nfoo 9 amd64 - < 9 amd64 - UNPACK"),
        ("candidate_version", "1.1 amd64 - < 2.0"),
        ("package_name", "foo bar"),
        ("architecture", "amd64; touch /tmp/x"),
    ):
        row = {
            "package_name": "foo",
            "architecture": "amd64",
            "installed_version": "1.0",
            "candidate_version": "1.1",
        }
        row[field] = value
        with pytest.raises(helper.MutationError):
            helper.build_expected_action_manifest(OPERATION_ID, [row])

    # And the generated script itself never embeds package material.
    script = helper.build_action_set_verifier(OPERATION_ID)
    for package in PACKAGES:
        assert package["package_name"] not in script
        assert package["candidate_version"] not in script


# ===========================================================================
# K. STAGING THE GATE INTO THE GUEST
# ===========================================================================


def test_staging_writes_the_gate_as_payload_bytes_under_a_wiped_root(
    host, journal
) -> None:
    """Fixed argv, payload on stdin, and no stale artifact left behind."""

    _prepare_then("execute_exact_package_mutation", host, journal)

    verifier = helper.guest_verifier_path(OPERATION_ID)
    manifest = helper.guest_manifest_path(OPERATION_ID)
    assert host.guest.files[verifier] == helper.build_action_set_verifier(
        OPERATION_ID
    ).encode("utf-8")
    assert host.guest.files[manifest] == helper.build_expected_action_manifest(
        OPERATION_ID, PACKAGES
    ).encode("utf-8")
    # Executable by root only, and the whole Hubinet root was cleared first.
    assert host.guest.modes[verifier] == "0500"
    assert helper.GUEST_STAGING_ROOT in host.guest.removed_roots

    # The bytes at the paths the real command will consult were read back and
    # digest-matched, which is what binds the gate to THIS operation rather
    # than to whatever happens to sit at the path.
    staged = helper.stage_action_set_gate(
        host,
        helper.validate_request(
            _request(
                "execute_exact_package_mutation",
                prepared_evidence_digest="0" * 64,
            )
        ),
        NODE,
    )
    assert staged == {
        manifest: hashlib.sha256(host.guest.files[manifest]).hexdigest(),
        verifier: hashlib.sha256(host.guest.files[verifier]).hexdigest(),
    }

    # No package name or version was ever an argument to any guest command.
    for argv in host.commands:
        for package in PACKAGES:
            assert package["package_name"] not in argv
            assert package["candidate_version"] not in argv


def test_a_stale_artifact_from_an_earlier_operation_is_removed_before_staging(
    host, journal
) -> None:
    stale = f"{helper.GUEST_STAGING_ROOT}/99999999-9999-4999-8999-999999999999/x"
    host.guest.files[stale] = b"leftover"

    _prepare_then("execute_exact_package_mutation", host, journal)

    assert stale not in host.guest.files
    assert helper.guest_verifier_path(OPERATION_ID) in host.guest.files


def test_a_gate_that_cannot_be_staged_refuses_before_submission(
    host, journal
) -> None:
    """Staging failure is a clean pre-submission refusal, still sealable."""

    host.guest.readonly = True

    prepared, response = _prepare_then(
        "execute_exact_package_mutation", host, journal
    )

    assert response["ok"] is False
    assert host.mutations == 0
    # Never crossed the submission boundary, so the operation stays at
    # `intent` and the durable seal can still release the job.
    assert journal.read(OPERATION_ID)["phase"] == "intent"


def test_staged_bytes_that_do_not_match_this_operation_refuse_before_submission(
    host, journal
) -> None:
    """The gate is integrity-bound to the operation, not to a path.

    If what now sits at the hook path is not what this operation produced,
    the mutation is refused rather than run under something else's gate.
    """

    original = host.guest.handle

    def _tamper(tail, stdin):
        result = original(tail, stdin)
        if len(tail) > 2 and tail[2] == "dd":
            host.guest.files[tail[3][3:]] = b"tampered\n"
        return result

    host.guest.handle = _tamper

    prepared, response = _prepare_then("execute_exact_package_mutation", host, journal)

    assert response["ok"] is False
    assert response["error"]["classification"] == "mutation_state_mismatch"
    assert host.mutations == 0
    assert journal.read(OPERATION_ID)["phase"] == "intent"


# ===========================================================================
# L. EVERY GUEST COMMAND REVALIDATES ITS OWN TARGET
#
# A VMID is an execution locator, not identity. PVE can free one and reuse it
# for an unrelated guest between any two commands, so "validate once, then
# run several commands" is not a safe shape: the second command can land in
# the replacement. The guest-command dispatcher owns the invariant, so no
# caller can amortize a check across two commands.
# ===========================================================================


class _TargetSwapper:
    """A live target that becomes a DIFFERENT guest between two commands.

    `swap_after` names the last guest command that still reaches the
    original guest. Everything the helper dispatches after it must be
    revalidated against the replacement -- which is precisely the window a
    caller that validated once and then ran several commands would miss.
    """

    def __init__(self, host, *, swap_after: str, row: dict) -> None:
        self.host = host
        self.swap_after = swap_after
        self.row = row
        self.guest_commands: list[tuple[str, ...]] = []
        self.swapped = False

    def __call__(self, argv, timeout, max_output, stdin=b""):
        argv = tuple(argv)
        if argv[2:3] == ("/cluster/resources",) and self.swapped:
            return helper.CommandResult(0, json.dumps([self.row]).encode(), b"")
        if argv[:2] == ("pct", "exec"):
            tail = argv[4:]
            self.guest_commands.append(tail)
            result = self.host(argv, timeout, max_output, stdin)
            if any(self.swap_after in word for word in tail):
                self.swapped = True
            return result
        return self.host(argv, timeout, max_output, stdin)


def test_the_target_changing_between_two_evidence_reads_refuses_the_second(
    host, journal
) -> None:
    """The dpkg inventory read is validated in its own right.

    The architecture read and the inventory read are two separate guest
    commands. Revalidating once and then issuing both would let the second
    land in a replacement guest.
    """

    swapper = _TargetSwapper(
        host,
        swap_after="--print-architecture",
        row={"vmid": VMID, "type": "lxc", "node": "pve-b", "status": "running"},
    )

    response = helper.handle_request(
        _request("prepare_exact_package_mutation"), runner=swapper, journal=journal
    )

    assert response["ok"] is False
    assert response["error"]["classification"] == "stale_target"
    # The architecture read reached the original guest; the inventory read
    # was never dispatched at all.
    assert helper.NATIVE_ARCHITECTURE_ARGV in swapper.guest_commands
    assert not any(
        "dpkg-query" in word for tail in swapper.guest_commands for word in tail
    )
    assert host.mutations == 0
    assert journal.read(OPERATION_ID) is None


def test_the_target_changing_before_staging_stages_nothing_into_the_wrong_guest(
    host, journal
) -> None:
    _handle(_request("prepare_exact_package_mutation"), host, journal)
    digest = journal.read(OPERATION_ID)["prepared_evidence_digest"]
    swapper = _TargetSwapper(
        host,
        swap_after="dpkg-query",
        row={"vmid": VMID, "type": "lxc", "node": "pve-b", "status": "running"},
    )

    response = _handle(
        _request("execute_exact_package_mutation", prepared_evidence_digest=digest),
        swapper,
        journal,
        spawn=_never_spawn,
    )

    assert response["ok"] is False
    assert response["error"]["classification"] == "stale_target"
    assert host.guest.files == {}
    assert host.mutations == 0
    # Never crossed the submission boundary.
    assert journal.read(OPERATION_ID)["phase"] == "intent"


def test_the_detached_runner_revalidates_immediately_before_the_real_command(
    host, journal
) -> None:
    """Finding C's witness: a VMID reused after `submitted` is durable.

    The runner performs its own fresh validation at the last practical
    instant. The mutation is never launched, the operation keeps its
    ownership and stays fenced, and it is NOT sealed as never-submitted --
    `submitted` was already durable, so the pre-submission release contract
    no longer applies.
    """

    _handle(_request("prepare_exact_package_mutation"), host, journal)
    digest = journal.read(OPERATION_ID)["prepared_evidence_digest"]

    swapper = _TargetSwapper(
        host,
        swap_after="sha256sum",
        row={"vmid": VMID, "type": "lxc", "node": NODE, "status": "stopped"},
    )

    response = _handle(
        _request("execute_exact_package_mutation", prepared_evidence_digest=digest),
        swapper,
        journal,
    )

    assert response["ok"] is True
    assert response["operation_state"] == "submitted"

    record = journal.read(OPERATION_ID)
    assert host.mutations == 0, "apt ran against a replacement guest"
    assert record["phase"] == "terminal_failure"
    assert record["phase"] != "sealed_not_submitted"
    assert record["result"]["exit_code"] != 0
    assert "guest is not running" in record["result"]["output_tail"]


def test_a_second_prepare_can_never_replace_the_accepted_intent(
    host, journal
) -> None:
    """Finding B's host half: prepared evidence is immutable once journaled.

    The digest an earlier PREPARE recorded may ALREADY be the one authority
    accepted and armed. Recomputing evidence and overwriting it is exactly
    how a concurrent PREPARE could replace the material the arming
    transaction bound itself to, so the second PREPARE is refused outright
    rather than served.
    """

    first = _handle(_request("prepare_exact_package_mutation"), host, journal)
    original_digest = first["evidence"]["prepared_evidence_digest"]

    # The guest's candidate state moves on, so a second PREPARE would
    # legitimately compute a DIFFERENT digest if it were allowed to.
    host.installed[("zlib1g", "amd64")] = "1.0.1"

    second = _handle(_request("prepare_exact_package_mutation"), host, journal)

    # Reported as the durable phase with NO evidence, so the backend can seal
    # and release rather than being stranded -- but never as evidence it
    # could arm with.
    assert second["operation_state"] == "intent"
    assert "evidence" not in second
    assert "immutable" in second["reason"]
    assert journal.read(OPERATION_ID)["prepared_evidence_digest"] == original_digest
    assert host.mutations == 0


def test_an_orphaned_intent_is_sealable_but_never_executable(
    host, journal
) -> None:
    """A PREPARE whose backend died is not permission to mutate.

    It cannot be re-prepared, it cannot be executed without the exact digest
    it recorded, and the existing pre-submission seal still resolves it.
    """

    prepared = _handle(_request("prepare_exact_package_mutation"), host, journal)
    digest = prepared["evidence"]["prepared_evidence_digest"]

    wrong = _handle(
        _request("execute_exact_package_mutation", prepared_evidence_digest="0" * 64),
        host,
        journal,
        spawn=_never_spawn,
    )
    assert wrong["ok"] is False
    assert wrong["error"]["classification"] == "mutation_state_mismatch"
    assert host.mutations == 0

    sealed = _handle(_request("seal_mutation_never_submitted"), host, journal)
    assert sealed["operation_state"] == "sealed_not_submitted"

    # And after the seal, even the correct digest can never mutate.
    after = _handle(
        _request("execute_exact_package_mutation", prepared_evidence_digest=digest),
        host,
        journal,
        spawn=_never_spawn,
    )
    assert after["operation_state"] == "sealed_not_submitted"
    assert host.mutations == 0


def test_a_staging_command_that_exits_early_is_a_failure_not_a_hang(
    tmp_path: Path,
) -> None:
    """A child that closes its stdin must not deadlock the boundary.

    The payload is written before the output pump starts, so a command that
    exits without draining it has to surface as an ordinary non-zero result
    rather than a blocked write.
    """

    payload = b"x" * (4 * 1024 * 1024)
    result = helper._run_bounded(
        (sys.executable, "-c", "raise SystemExit(3)"), 30.0, 64 * 1024, payload
    )

    assert result.returncode == 3
    assert result.timed_out is False
