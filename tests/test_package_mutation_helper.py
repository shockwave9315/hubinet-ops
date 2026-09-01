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

import importlib.util
import json
import os
from pathlib import Path
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


class FakeHost:
    """Answers exactly the fixed argv shapes the helper issues."""

    def __init__(self) -> None:
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

    def __call__(self, argv, timeout, max_output):
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
            assert tuple(tail) == helper.MUTATION_ARGV
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
    argv = helper.MUTATION_ARGV
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

    def _fake_runner(argv, timeout, max_output):
        if tuple(argv[4:])[-1] == "upgrade" and "-s" not in argv:
            marker.write_text(str(os.getpid()), encoding="utf-8")
        return host(argv, timeout, max_output)

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

    def _moved(argv, timeout, max_output):
        if tuple(argv)[2:3] == ("/cluster/resources",):
            return helper.CommandResult(0, json.dumps([row]).encode(), b"")
        return original(argv, timeout, max_output)

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
