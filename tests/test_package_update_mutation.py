"""Crash-safe real package mutation: authority, orchestration, and recovery.

Covers the whole boundary between a job's confirmed pre-update snapshot and
one proven, at-most-once, job-owned workload package mutation:

- the write-ahead ``mutation_may_have_started`` uncertainty boundary and its
  SQL invariants;
- the bounded submission critical section and its durable release proof;
- the crash/race matrix (crash before submission, during submission, after a
  terminal result, restart while running, ambiguous evidence, a delayed
  request racing a recovery seal, a moved or replaced resource);
- the package-manager race fence: a package-state change between the
  execution-time simulation and the real command can never silently produce
  a materially different mutation;
- the independent dpkg completion proof.

Nothing here runs a real ``apt``, ``pct``, ``ssh``, or PVE operation. The
host boundary is the actual dark helper module driven by a fake guest, with
its detached runner replaced by an explicit test double, an isolated
temporary journal directory, and a JSON round trip through the real
transport parser.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
from types import ModuleType
import uuid

import pytest

from app.inventory import (
    AuthorityConflict,
    HostMutationState,
    InventoryAuthority,
    InventoryAuthorityStore,
    PackageScanFailure,
    PackageScanPackage,
    PackageUpdateCheckpoint,
    PackageUpdateEventType,
    PackageMutationArmOutcome,
    PackageMutationEvidenceNotAccepted,
    PackageUpdateExecutionOutcome,
    PackageUpdateJobStatus,
)
from app.inventory.mutation_completion import (
    PackageMutationPostState,
    prove_package_mutation_completion,
)
from app.inventory.mutation_identity import derive_package_mutation_identity
from app.package_update_mutation import (
    HostMutationResult,
    MutationStageStatus,
    PackageUpdateMutationOrchestrator,
)
from app.package_update_mutation_host_control import (
    build_host_request,
    parse_host_response,
)
from tests.test_package_update_execution_gate import (
    _inventory_for,
    _ready_job,
    _simulation_for,
)
from tests.test_package_update_job_authority import _issue
from tests.test_package_update_snapshot_safety import _canonical


ROOT = Path(__file__).resolve().parents[1]
HELPER_PATH = ROOT / "deploy" / "hubinet-package-mutation-helper.py"


def _load_helper() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "hubinet_package_mutation_helper", HELPER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


helper = _load_helper()

NODE = "pve-a"
OS_RELEASE = 'ID=debian\nVERSION_ID="12"\n'
APT_VERSION = "apt 2.6.1 (amd64)\n"

#: Packages the shared fixtures approve, plus enough unrelated installed
#: packages that "nothing else changed" is a real assertion, not a vacuous
#: one over a two-row system.
BACKGROUND_PACKAGES = (
    ("bash", "amd64", "5.2.15-2"),
    ("coreutils", "amd64", "9.1-1"),
    ("libfoo", "i386", "1.0-1"),
    ("tzdata", "all", "2023c-5"),
)


# ===========================================================================
# A fake guest, driven through the REAL dark helper
# ===========================================================================


#: A well-formed prepared-evidence digest for tests that arm authority
#: directly rather than through a real host PREPARE. Its VALUE is arbitrary;
#: what matters is that authority now refuses to arm without one and refuses
#: to submit with a different one.
ARBITRARY_EVIDENCE_DIGEST = "a" * 64
OTHER_EVIDENCE_DIGEST = "b" * 64


class FakeGuest:
    """A deterministic stand-in for one running Debian LXC guest.

    It answers exactly the fixed argv shapes the dark helper issues, and
    nothing else: an unrecognised command is a test failure, not a silent
    empty result. Its whole point is that the helper's own journal, lease,
    and refusal logic run for real against it.
    """

    def __init__(
        self,
        *,
        approved: tuple[PackageScanPackage, ...],
        vmid: int,
        node: str = NODE,
    ) -> None:
        self.vmid = vmid
        self.node = node
        self.approved = approved
        self.installed: dict[tuple[str, str], str] = {
            (package.package_name, package.architecture): package.installed_version
            for package in approved
        }
        self.installed.update(
            {(name, architecture): version for name, architecture, version in
             BACKGROUND_PACKAGES}
        )
        self.unfinished: dict[tuple[str, str], str] = {}
        #: The guest-side files the helper stages, keyed by absolute path.
        #: Contents arrive as stdin payload bytes, never as command text.
        self.staged_files: dict[str, bytes] = {}
        #: Set by the test once the job's operation identity is known, so
        #: the fake can assert the real command carries that operation's own
        #: action gate.
        self.mutation_operation_id: str | None = None
        #: What a fresh `apt-get -s upgrade` would report. Defaults to the
        #: approved plan; tests move it to model a real package-manager race.
        self.simulated = approved
        self.native_architecture = "amd64\n"
        self.present = True
        self.running = True
        self.resource_type = "lxc"
        self.current_node = node
        self.simulation_returncode = 0
        self.simulation_stderr = ""
        self.update_returncode = 0
        self.update_stderr = ""
        #: Recorded real package mutations. MUST never exceed one per test.
        self.mutations: list[int] = []
        self.mutation_exit_code = 0
        self.mutation_applies = True
        self.mutation_extra: dict[tuple[str, str], str] = {}
        self.mutation_removes: tuple[tuple[str, str], ...] = ()
        self.mutation_leaves_unfinished: tuple[tuple[str, str, str], ...] = ()
        self.post_state_unreadable = False

    # -- rendering -----------------------------------------------------

    def inventory_text(self) -> str:
        if self.post_state_unreadable and self.mutations:
            return ""
        # dpkg reports each (name, architecture) exactly once, in exactly one
        # state, so a mid-transaction package is not also listed as installed.
        rows = [
            f"{name}\t{architecture}\t{version}\tinstalled\n"
            for (name, architecture), version in sorted(self.installed.items())
            if (name, architecture) not in self.unfinished
        ]
        rows.extend(
            f"{name}\t{architecture}\t{version}\t{status}\n"
            for (name, architecture), (version, status) in sorted(
                self.unfinished.items()
            )
        )
        return "".join(rows)

    def simulation_text(self) -> str:
        text = _simulation_for(self.simulated)
        if self.unfinished:
            # APT's own summary printer (`apt-private/private-output.cc::Stats`)
            # appends this line whenever dpkg reports a nonzero
            # broken/not-fully-installed count. Emitting it is what makes this
            # fixture realistic rather than impossible output.
            text += f"{len(self.unfinished)} not fully installed or removed.\n"
        return text

    # -- the one real mutation ----------------------------------------

    def _apply_mutation(self) -> int:
        self.mutations.append(len(self.mutations) + 1)
        if self.mutation_applies:
            for package in self.simulated:
                self.installed[
                    (package.package_name, package.architecture)
                ] = package.candidate_version
        self.installed.update(self.mutation_extra)
        for identity in self.mutation_removes:
            self.installed.pop(identity, None)
        for name, architecture, status in self.mutation_leaves_unfinished:
            self.unfinished[(name, architecture)] = (
                self.installed.pop((name, architecture), "0"),
                status,
            )
        return self.mutation_exit_code

    # -- the runner the helper calls ----------------------------------

    def runner(self, argv, timeout, max_output, stdin=b""):
        argv = tuple(argv)
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
            return self._guest(argv[4:], stdin)
        raise AssertionError(f"unexpected host command: {argv}")

    def _guest(self, tail, stdin=b""):
        staged = self._staging(tail, stdin)
        if staged is not None:
            return staged
        if tail[-1] == "/etc/os-release":
            return self._ok(OS_RELEASE.encode())
        if tail[-1] == "--version":
            return self._ok(APT_VERSION.encode())
        if tail[-1] == "--print-architecture":
            return self._ok(self.native_architecture.encode())
        if tail[2] == "dpkg-query":
            return self._ok(self.inventory_text().encode())
        if "update" in tail:
            return helper.CommandResult(
                self.update_returncode, b"", self.update_stderr.encode()
            )
        if "-s" in tail and tail[-1] == "upgrade":
            return helper.CommandResult(
                self.simulation_returncode,
                self.simulation_text().encode(),
                self.simulation_stderr.encode(),
            )
        if tail[-1] == "upgrade":
            assert tuple(tail) == helper.mutation_argv(self.mutation_operation_id), tail
            # The real gate lives in the guest. A fake that mutated without
            # one staged would be proving something that cannot happen.
            verifier = helper.guest_verifier_path(self.mutation_operation_id)
            assert verifier in self.staged_files, "mutation ran with no action gate"
            return helper.CommandResult(self._apply_mutation(), b"upgraded\n", b"")
        raise AssertionError(f"unexpected guest command: {tail}")

    def _staging(self, tail, stdin):
        """Model the fixed staging argv shapes, payload on stdin only."""

        command = tail[2] if len(tail) > 2 else ""
        if command == "rm":
            root = tail[-1]
            for path in [p for p in self.staged_files if p.startswith(root)]:
                del self.staged_files[path]
            return self._ok(b"")
        if command == "mkdir":
            return self._ok(b"")
        if command == "dd":
            assert tail[3].startswith("of="), tail
            self.staged_files[tail[3][3:]] = stdin
            return self._ok(b"")
        if command == "chmod":
            return self._ok(b"")
        if command == "sha256sum":
            lines = []
            for path in tail[3:]:
                if path not in self.staged_files:
                    return helper.CommandResult(1, b"", b"No such file\n")
                digest = hashlib.sha256(self.staged_files[path]).hexdigest()
                lines.append(f"{digest}  {path}\n")
            return self._ok("".join(lines).encode())
        return None

    @staticmethod
    def _ok(stdout: bytes):
        return helper.CommandResult(0, stdout, b"")


class HelperBackedHostControl:
    """Drive the REAL dark helper over a JSON round trip, hermetically.

    Every request is rendered by the production transport's own request
    builder, handed to the helper's `handle_request`, serialized to JSON, and
    parsed by the production transport's own response parser. Only the
    process boundary (SSH) and the detached fork are replaced.
    """

    def __init__(self, guest: FakeGuest, journal_directory: Path) -> None:
        self.guest = guest
        # `journal_directory.parent` stands in for `JOURNAL_DURABILITY_ANCHOR`
        # (`/var/lib` in production): the caller's `tmp_path`-rooted parent is
        # exactly as durable a pre-existing boundary for this test as the
        # real anchor is in production.
        self.journal = helper.OperationJournal(
            journal_directory, anchor=journal_directory.parent
        )
        self.calls: list[str] = []
        #: How the detached runner behaves. "run" completes it synchronously,
        #: "hold" models one still running, "die" models a runner killed
        #: before it could journal anything.
        self.runner_mode = "run"
        self._held_descriptors: list[int] = []
        self.fail_operation: dict[str, BaseException] = {}
        self.drop_response: set[str] = set()

    # -- lifecycle -----------------------------------------------------

    def release_held_runner(self) -> None:
        """Simulate a held runner exiting without a terminal record."""

        for descriptor in self._held_descriptors:
            os.close(descriptor)
        self._held_descriptors.clear()

    def finish_held_runner(self, request) -> None:
        """Simulate a held runner completing and journaling its result."""

        payload = build_host_request(request, "execute_exact_package_mutation")
        parsed = helper.validate_request(
            {**payload, "prepared_evidence_digest": "0" * 64}
        )
        record = self.journal.read(parsed["mutation_operation_id"])
        assert record is not None and record["phase"] == "submitted"
        pre_native, pre_inventory = self._captured_pre_state
        helper.run_mutation(
            parsed,
            self.journal,
            local_node=self.guest.node,
            pre_native_architecture=pre_native,
            pre_installed_inventory=pre_inventory,
            runner=self.guest.runner,
        )
        self.release_held_runner()

    # -- the four typed operations ------------------------------------

    def prepare_exact_package_mutation(self, request):
        return self._call(request, "prepare_exact_package_mutation")

    def execute_exact_package_mutation(self, request, *, prepared_evidence_digest):
        return self._call(
            request,
            "execute_exact_package_mutation",
            prepared_evidence_digest=prepared_evidence_digest,
        )

    def seal_mutation_never_submitted(self, request):
        return self._call(request, "seal_mutation_never_submitted")

    def inspect_package_mutation_state(self, request):
        return self._call(request, "inspect_package_mutation_state")

    # -- plumbing ------------------------------------------------------

    def _call(self, request, operation, *, prepared_evidence_digest=None):
        self.calls.append(operation)
        # The guest fake asserts the real command carries THIS operation's
        # own action gate, so it needs the identity the backend derived.
        self.guest.mutation_operation_id = request.mutation_operation_id
        if operation in self.fail_operation:
            raise self.fail_operation[operation]
        payload = build_host_request(
            request, operation, prepared_evidence_digest=prepared_evidence_digest
        )
        original_spawn = helper._spawn_detached_runner
        helper._spawn_detached_runner = self._spawn
        try:
            response = helper.handle_request(
                payload, runner=self.guest.runner, journal=self.journal
            )
        finally:
            helper._spawn_detached_runner = original_spawn
        # Prove the whole answer survives a real serialization boundary.
        response = json.loads(json.dumps(response, ensure_ascii=True))
        if operation in self.drop_response:
            raise TimeoutError("host-control request timed out")
        return parse_host_response(response, payload)

    def _spawn(
        self,
        request,
        journal,
        lease,
        *,
        local_node,
        pre_native_architecture,
        pre_installed_inventory,
        runner,
    ) -> None:
        """Stand in for the double-forked runner, without forking.

        Hands the per-VMID lease over exactly the way the real code does --
        the lock lives on the open file description, so duplicating the
        descriptor keeps it held after the caller detaches -- so the
        "a mutation is running right now" evidence path is the real one.
        """

        self._captured_pre_state = (pre_native_architecture, pre_installed_inventory)
        descriptor = lease.descriptor
        assert descriptor is not None
        duplicate = os.dup(descriptor)
        lease.detach()
        if self.runner_mode == "die":
            os.close(duplicate)
            return
        if self.runner_mode == "hold":
            self._held_descriptors.append(duplicate)
            return
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


def _armed_system(tmp_path: Path, *, packages=None):
    """One ACTIVE job at snapshot_confirmed, plus a matching fake guest."""

    clock, store, authority, resource, scan, approval, job = _ready_job(
        tmp_path, packages=packages
    )
    guest = FakeGuest(approved=job.packages, vmid=job.expected_vmid)
    guest.simulated = job.packages
    host = HelperBackedHostControl(guest, tmp_path / "journal")
    orchestrator = _orchestrator(authority, host)
    return clock, store, authority, resource, scan, approval, job, guest, host, orchestrator


class _FakeMonotonic:
    """A deterministic clock so bounded polling terminates in a test.

    Real polling of a running mutation is bounded by wall-clock time; a test
    must exercise that bound without ever sleeping, so time advances by one
    poll interval per observation.
    """

    def __init__(self, step: float) -> None:
        self._now = 0.0
        self._step = step

    def __call__(self) -> float:
        self._now += self._step
        return self._now


def _orchestrator(authority, host, *, poll_timeout_seconds: float = 30.0):
    return PackageUpdateMutationOrchestrator(
        authority,
        host,
        sleep=lambda _seconds: None,
        monotonic=_FakeMonotonic(5.0),
        poll_timeout_seconds=poll_timeout_seconds,
        poll_interval_seconds=5.0,
    )


def _events(store, job_id):
    return [
        event.event_type
        for event in store.list_package_update_job_events(job_id, limit=200)
    ]


# ===========================================================================
# A. DETERMINISTIC, JOB-OWNED MUTATION IDENTITY
# ===========================================================================


def test_same_job_derives_the_same_mutation_identity_across_restart(
    tmp_path: Path,
) -> None:
    clock, store, authority, _, _, _, job, *_ = _armed_system(tmp_path)

    first = authority.package_update_mutation_identity(job.job_id)
    path = store.path
    store.close()
    reopened = InventoryAuthorityStore(path, now=clock)
    second = InventoryAuthority(reopened, now=clock).package_update_mutation_identity(
        job.job_id
    )
    assert first == second
    reopened.close()


def test_mutation_identity_binds_backend_job_resource_and_incarnation() -> None:
    base = {
        "backend_instance_id": "11111111-1111-4111-8111-111111111111",
        "job_id": "22222222-2222-4222-8222-222222222222",
        "resource_id": "33333333-3333-4333-8333-333333333333",
        "resource_continuity_revision": 1,
    }
    identity = derive_package_mutation_identity(**base)
    for field, replacement in (
        ("backend_instance_id", "44444444-4444-4444-8444-444444444444"),
        ("job_id", "55555555-5555-4555-8555-555555555555"),
        ("resource_id", "66666666-6666-4666-8666-666666666666"),
        ("resource_continuity_revision", 2),
    ):
        other = derive_package_mutation_identity(**{**base, field: replacement})
        assert other != identity, field


def test_a_mutation_identity_never_collides_with_the_snapshot_identity() -> None:
    from app.inventory.snapshot_identity import derive_pre_update_snapshot_identity

    facts = {
        "backend_instance_id": "11111111-1111-4111-8111-111111111111",
        "job_id": "22222222-2222-4222-8222-222222222222",
        "resource_id": "33333333-3333-4333-8333-333333333333",
        "resource_continuity_revision": 1,
    }
    snapshot = derive_pre_update_snapshot_identity(**facts)
    mutation = derive_package_mutation_identity(**facts)
    assert mutation.mutation_operation_id != snapshot.snapshot_operation_id


# ===========================================================================
# B. THE WRITE-AHEAD UNCERTAINTY BOUNDARY
# ===========================================================================


def test_arming_commits_the_write_ahead_boundary_with_its_identity(
    tmp_path: Path,
) -> None:
    _, store, authority, _, _, _, job, guest, host, orchestrator = _armed_system(
        tmp_path
    )
    identity = authority.package_update_mutation_identity(job.job_id)

    outcome, arm, armed = authority.arm_package_update_mutation(
        job.job_id,
        job_packages(job),
        prepared_evidence_digest=ARBITRARY_EVIDENCE_DIGEST,
    )

    assert outcome is PackageUpdateExecutionOutcome.MATCHED
    # THIS invocation committed the boundary, so only it may submit.
    assert arm is PackageMutationArmOutcome.ARMED_NOW
    assert armed.accepted_prepared_evidence_digest == ARBITRARY_EVIDENCE_DIGEST
    assert armed.checkpoint is PackageUpdateCheckpoint.MUTATION_MAY_HAVE_STARTED
    assert armed.status is PackageUpdateJobStatus.ACTIVE
    assert armed.mutation_operation_id == identity.mutation_operation_id
    assert armed.mutation_may_have_started_at is not None
    assert armed.mutation_completed_at is None
    assert PackageUpdateEventType.MUTATION_MAY_HAVE_STARTED in _events(
        store, job.job_id
    )
    # Zero host calls: arming is a pure authority transition.
    assert host.calls == []
    assert guest.mutations == []


def job_packages(job) -> tuple[PackageScanPackage, ...]:
    return tuple(
        PackageScanPackage(
            package_name=package.package_name,
            architecture=package.architecture,
            installed_version=package.installed_version,
            candidate_version=package.candidate_version,
        )
        for package in job.packages
    )


def test_arming_is_idempotent_and_never_creates_a_second_boundary(
    tmp_path: Path,
) -> None:
    _, store, authority, _, _, _, job, _, _, _ = _armed_system(tmp_path)
    _, first_arm, first = authority.arm_package_update_mutation(
        job.job_id,
        job_packages(job),
        prepared_evidence_digest=ARBITRARY_EVIDENCE_DIGEST,
    )
    assert first_arm is PackageMutationArmOutcome.ARMED_NOW
    # A second arming -- even one carrying DIFFERENT evidence -- re-proves
    # the identity and returns the existing durable boundary. It never
    # replaces the accepted digest, and it never claims the right to submit.
    outcome, second_arm, second = authority.arm_package_update_mutation(
        job.job_id,
        job_packages(job),
        prepared_evidence_digest=OTHER_EVIDENCE_DIGEST,
    )
    assert outcome is PackageUpdateExecutionOutcome.MATCHED
    assert second_arm is PackageMutationArmOutcome.ALREADY_ARMED
    assert second.accepted_prepared_evidence_digest == ARBITRARY_EVIDENCE_DIGEST
    assert second.mutation_may_have_started_at == first.mutation_may_have_started_at
    assert second.mutation_operation_id == first.mutation_operation_id
    assert (
        _events(store, job.job_id).count(
            PackageUpdateEventType.MUTATION_MAY_HAVE_STARTED
        )
        == 1
    )


def test_arming_requires_a_confirmed_snapshot(tmp_path: Path) -> None:
    from tests.test_package_update_job_authority import _approved_system

    _, _, authority, resource, _, approval = _approved_system(tmp_path)
    job = _issue(authority, resource, approval)
    with pytest.raises(AuthorityConflict, match="not awaiting the package mutation"):
        authority.arm_package_update_mutation(
        job.job_id,
        job_packages(job),
        prepared_evidence_digest=ARBITRARY_EVIDENCE_DIGEST,
    )
    assert (
        authority.package_update_job(job.job_id).mutation_may_have_started_at is None
    )


# ===========================================================================
# C. THE HAPPY PATH, PROVEN INDEPENDENTLY OF THE EXIT CODE
# ===========================================================================


def test_one_proven_mutation_completes_exactly_once_and_stays_active(
    tmp_path: Path,
) -> None:
    _, store, authority, _, _, _, job, guest, host, orchestrator = _armed_system(
        tmp_path
    )

    result = orchestrator.execute_job_owned_mutation(job.job_id)

    assert result.status is MutationStageStatus.COMPLETED
    assert guest.mutations == [1]
    completed = store.package_update_job(job.job_id)
    assert completed.checkpoint is PackageUpdateCheckpoint.MUTATION_COMPLETED
    assert completed.mutation_completed_at is not None
    # Completion is NOT job success: the healthcheck has not happened.
    assert completed.status is PackageUpdateJobStatus.ACTIVE
    assert completed.terminalized_at is None
    # The job keeps its snapshot identity and rollback authority throughout.
    assert completed.snapshot_name == job.snapshot_name
    assert completed.snapshot_operation_id == job.snapshot_operation_id
    assert completed.snapshot_confirmed_at == job.snapshot_confirmed_at
    events = _events(store, job.job_id)
    assert PackageUpdateEventType.MUTATION_MAY_HAVE_STARTED in events
    assert PackageUpdateEventType.MUTATION_SUBMITTED in events
    assert PackageUpdateEventType.MUTATION_COMPLETED in events


def test_a_second_orchestration_never_runs_a_second_mutation(
    tmp_path: Path,
) -> None:
    _, store, authority, _, _, _, job, guest, host, orchestrator = _armed_system(
        tmp_path
    )
    assert (
        orchestrator.execute_job_owned_mutation(job.job_id).status
        is MutationStageStatus.COMPLETED
    )

    again = orchestrator.execute_job_owned_mutation(job.job_id)

    assert again.status is MutationStageStatus.COMPLETED
    assert guest.mutations == [1]


def test_completion_requires_every_approved_row_at_its_exact_candidate(
    tmp_path: Path,
) -> None:
    _, store, authority, _, _, _, job, guest, host, orchestrator = _armed_system(
        tmp_path
    )
    # apt exits 0 but silently leaves the packages where they were.
    guest.mutation_applies = False

    result = orchestrator.execute_job_owned_mutation(job.job_id)

    assert result.status is MutationStageStatus.UNCERTAIN
    fenced = store.package_update_job(job.job_id)
    assert fenced.status is PackageUpdateJobStatus.ACTIVE
    assert fenced.checkpoint is PackageUpdateCheckpoint.MUTATION_MAY_HAVE_STARTED
    assert fenced.mutation_completed_at is None
    assert guest.mutations == [1]


def test_completion_refuses_when_an_unapproved_package_also_changed(
    tmp_path: Path,
) -> None:
    _, store, authority, _, _, _, job, guest, host, orchestrator = _armed_system(
        tmp_path
    )
    guest.mutation_extra = {("bash", "amd64"): "5.2.15-3"}

    result = orchestrator.execute_job_owned_mutation(job.job_id)

    assert result.status is MutationStageStatus.UNCERTAIN
    assert "outside the approved plan" in (result.reason or "")
    assert (
        store.package_update_job(job.job_id).checkpoint
        is PackageUpdateCheckpoint.MUTATION_MAY_HAVE_STARTED
    )


def test_completion_refuses_a_package_that_disappeared(tmp_path: Path) -> None:
    _, store, authority, _, _, _, job, guest, host, orchestrator = _armed_system(
        tmp_path
    )
    guest.mutation_removes = (("libfoo", "i386"),)

    result = orchestrator.execute_job_owned_mutation(job.job_id)

    assert result.status is MutationStageStatus.UNCERTAIN
    assert "no longer installed" in (result.reason or "")


def test_completion_refuses_unfinished_dpkg_state(tmp_path: Path) -> None:
    _, store, authority, _, _, _, job, guest, host, orchestrator = _armed_system(
        tmp_path
    )
    guest.mutation_leaves_unfinished = (("coreutils", "amd64", "half-configured"),)

    result = orchestrator.execute_job_owned_mutation(job.job_id)

    assert result.status is MutationStageStatus.UNCERTAIN
    assert "unfinished" in (result.reason or "")
    assert (
        store.package_update_job(job.job_id).checkpoint
        is PackageUpdateCheckpoint.MUTATION_MAY_HAVE_STARTED
    )


def test_completion_refuses_when_the_post_state_cannot_be_read(
    tmp_path: Path,
) -> None:
    _, store, authority, _, _, _, job, guest, host, orchestrator = _armed_system(
        tmp_path
    )
    guest.post_state_unreadable = True

    result = orchestrator.execute_job_owned_mutation(job.job_id)

    # An empty post-state is a terminal FAILURE of the operation, not a
    # clean exit: an exit code with no independent evidence proves nothing.
    assert result.status is MutationStageStatus.TERMINAL_FAILURE
    fenced = store.package_update_job(job.job_id)
    assert fenced.status is PackageUpdateJobStatus.ACTIVE
    assert fenced.mutation_completed_at is None


# ===========================================================================
# D. THE PACKAGE-MANAGER RACE FENCE
# ===========================================================================


def test_a_changed_plan_at_execution_time_blocks_without_mutating(
    tmp_path: Path,
) -> None:
    _, store, authority, _, _, _, job, guest, host, orchestrator = _armed_system(
        tmp_path
    )
    # Another actor published a newer candidate between approval and now.
    guest.simulated = tuple(
        PackageScanPackage(
            package_name=package.package_name,
            architecture=package.architecture,
            installed_version=package.installed_version,
            candidate_version=package.candidate_version + "u1",
        )
        for package in job.packages
    )

    result = orchestrator.execute_job_owned_mutation(job.job_id)

    assert result.status is MutationStageStatus.MISMATCHED
    assert guest.mutations == []
    blocked = store.package_update_job(job.job_id)
    assert blocked.status is PackageUpdateJobStatus.BLOCKED
    assert blocked.checkpoint is PackageUpdateCheckpoint.SNAPSHOT_CONFIRMED
    assert blocked.mutation_may_have_started_at is None
    assert blocked.mutation_operation_id is None
    # The snapshot is retained but grants no rollback authority.
    assert blocked.snapshot_confirmed_at is not None


def test_a_package_held_back_at_execution_time_blocks_without_mutating(
    tmp_path: Path,
) -> None:
    """A package that stops being upgradable is a materially different plan."""

    _, store, authority, _, _, _, job, guest, host, orchestrator = _armed_system(
        tmp_path
    )
    assert len(job.packages) > 1
    guest.simulated = job.packages[:1]

    result = orchestrator.execute_job_owned_mutation(job.job_id)

    assert result.status is MutationStageStatus.MISMATCHED
    assert guest.mutations == []
    blocked = store.package_update_job(job.job_id)
    assert blocked.status is PackageUpdateJobStatus.BLOCKED
    assert blocked.mutation_may_have_started_at is None


def test_authority_going_stale_between_arming_and_submission_seals(
    tmp_path: Path,
) -> None:
    """The submission critical section's own race, closed.

    Discovery can invalidate this job's resource incarnation after the
    write-ahead boundary is committed and before the host is asked to
    mutate. Current authority is re-proved inside the same transaction as
    the submission, so the host is never asked -- and the durable seal then
    releases the global slot instead of fencing it forever.
    """

    _, store, authority, resource, _, _, job, guest, host, orchestrator = (
        _armed_system(tmp_path)
    )
    original = authority.arm_package_update_mutation

    def _arm_then_drift(job_id, fresh_packages, *, prepared_evidence_digest):
        outcome, arm, armed = original(
            job_id, fresh_packages, prepared_evidence_digest=prepared_evidence_digest
        )
        authority.rotate_transport_trust(resource.inventory_source_id)
        return outcome, arm, armed

    authority.arm_package_update_mutation = _arm_then_drift

    result = orchestrator.execute_job_owned_mutation(job.job_id)

    assert guest.mutations == []
    assert "execute_exact_package_mutation" not in host.calls
    assert result.status is MutationStageStatus.NOT_SUBMITTED
    released = store.package_update_job(job.job_id)
    assert released.status is PackageUpdateJobStatus.BLOCKED
    assert released.checkpoint is PackageUpdateCheckpoint.MUTATION_MAY_HAVE_STARTED


def test_a_package_upgraded_by_someone_else_between_proof_and_command_refuses(
    tmp_path: Path,
) -> None:
    """The host's own last-instant installed-state fence.

    The backend proved the exact plan, armed the boundary, and asked the host
    to mutate. Between those two host round trips another actor upgraded one
    of the approved packages. The mutation the operator approved is no longer
    the mutation that would happen, so the host refuses before launching
    anything.
    """

    _, store, authority, _, _, _, job, guest, host, orchestrator = _armed_system(
        tmp_path
    )
    approved = job.packages[0]

    def _drift(request, operation, **kwargs):
        if operation == "execute_exact_package_mutation":
            guest.installed[
                (approved.package_name, approved.architecture)
            ] = "9:9.9-9"
        return original(request, operation, **kwargs)

    original = host._call
    host._call = _drift

    result = orchestrator.execute_job_owned_mutation(job.job_id)

    assert guest.mutations == []
    assert result.status is MutationStageStatus.UNCERTAIN
    fenced = store.package_update_job(job.job_id)
    # The write-ahead boundary already existed, so the job stays fenced and
    # owns the slot: the host refusing is not proof that nothing happened.
    assert fenced.status is PackageUpdateJobStatus.ACTIVE
    assert fenced.checkpoint is PackageUpdateCheckpoint.MUTATION_MAY_HAVE_STARTED


def test_pre_existing_unfinished_dpkg_state_refuses_before_any_mutation(
    tmp_path: Path,
) -> None:
    _, store, authority, _, _, _, job, guest, host, orchestrator = _armed_system(
        tmp_path
    )
    guest.unfinished[("coreutils", "amd64")] = ("9.1-1", "half-configured")

    result = orchestrator.execute_job_owned_mutation(job.job_id)

    assert guest.mutations == []
    # The canonical parser fails the plan closed before anything is armed.
    assert result.status is MutationStageStatus.HOST_FAILURE
    assert result.failure_class is PackageScanFailure.MALFORMED_PLAN
    untouched = store.package_update_job(job.job_id)
    assert untouched.checkpoint is PackageUpdateCheckpoint.SNAPSHOT_CONFIRMED
    assert untouched.mutation_may_have_started_at is None


def test_unfinished_dpkg_state_appearing_after_arming_refuses_at_the_host(
    tmp_path: Path,
) -> None:
    """The host's own last-instant fence, independent of the plan parser.

    If the guest becomes mid-transaction only after the backend proved the
    plan and armed the boundary, the canonical parser never saw it. The host
    re-reads dpkg under its own lease immediately before launching and
    refuses, so no package command runs.
    """

    _, store, authority, _, _, _, job, guest, host, orchestrator = _armed_system(
        tmp_path
    )
    original = host._call

    def _break_dpkg(request, operation, **kwargs):
        if operation == "execute_exact_package_mutation":
            guest.unfinished[("coreutils", "amd64")] = ("9.1-1", "half-configured")
        return original(request, operation, **kwargs)

    host._call = _break_dpkg

    result = orchestrator.execute_job_owned_mutation(job.job_id)

    assert guest.mutations == []
    assert result.status is MutationStageStatus.UNCERTAIN
    fenced = store.package_update_job(job.job_id)
    assert fenced.status is PackageUpdateJobStatus.ACTIVE
    assert fenced.checkpoint is PackageUpdateCheckpoint.MUTATION_MAY_HAVE_STARTED


def test_a_busy_package_manager_never_arms_or_mutates(tmp_path: Path) -> None:
    _, store, authority, _, _, _, job, guest, host, orchestrator = _armed_system(
        tmp_path
    )
    guest.update_returncode = 100
    guest.update_stderr = "E: Could not get lock /var/lib/dpkg/lock-frontend"

    result = orchestrator.execute_job_owned_mutation(job.job_id)

    assert result.status is MutationStageStatus.HOST_FAILURE
    assert result.failure_class is PackageScanFailure.PACKAGE_MANAGER_BUSY
    assert guest.mutations == []
    assert (
        store.package_update_job(job.job_id).mutation_may_have_started_at is None
    )


# ===========================================================================
# E. AUTHORITY STALENESS AND TARGET REVALIDATION
# ===========================================================================


def test_a_stale_authority_context_releases_without_mutating(
    tmp_path: Path,
) -> None:
    _, store, authority, resource, _, _, job, guest, host, orchestrator = (
        _armed_system(tmp_path)
    )
    authority.rotate_transport_trust(resource.inventory_source_id)

    result = orchestrator.execute_job_owned_mutation(job.job_id)

    assert result.status is MutationStageStatus.AUTHORITY_STALE
    assert guest.mutations == []
    released = store.package_update_job(job.job_id)
    assert released.status is PackageUpdateJobStatus.BLOCKED
    assert released.mutation_may_have_started_at is None
    assert released.checkpoint is PackageUpdateCheckpoint.SNAPSHOT_CONFIRMED


def test_a_moved_guest_never_mutates(tmp_path: Path) -> None:
    _, store, authority, _, _, _, job, guest, host, orchestrator = _armed_system(
        tmp_path
    )
    guest.current_node = "pve-b"

    result = orchestrator.execute_job_owned_mutation(job.job_id)

    assert guest.mutations == []
    assert result.status is MutationStageStatus.HOST_FAILURE
    assert result.failure_class is PackageScanFailure.STALE_TARGET
    assert (
        store.package_update_job(job.job_id).mutation_may_have_started_at is None
    )


def test_a_replaced_resource_type_never_mutates(tmp_path: Path) -> None:
    _, store, authority, _, _, _, job, guest, host, orchestrator = _armed_system(
        tmp_path
    )
    guest.resource_type = "qemu"

    result = orchestrator.execute_job_owned_mutation(job.job_id)

    assert guest.mutations == []
    assert result.failure_class is PackageScanFailure.UNSUPPORTED_RESOURCE_TYPE


def test_a_guest_that_disappears_after_submission_preserves_uncertainty(
    tmp_path: Path,
) -> None:
    _, store, authority, _, _, _, job, guest, host, orchestrator = _armed_system(
        tmp_path
    )
    host.runner_mode = "hold"
    first = orchestrator.execute_job_owned_mutation(job.job_id)
    assert first.status is MutationStageStatus.RUNNING
    assert guest.mutations == []

    # The guest vanishes while the mutation is in flight. The durable host
    # journal is read WITHOUT touching PVE, so uncertainty is preserved
    # rather than turned into a false "never happened".
    guest.present = False
    recovered = orchestrator.recover_job_owned_mutation(job.job_id)

    assert recovered.status is MutationStageStatus.RUNNING
    fenced = store.package_update_job(job.job_id)
    assert fenced.status is PackageUpdateJobStatus.ACTIVE
    assert fenced.checkpoint is PackageUpdateCheckpoint.MUTATION_MAY_HAVE_STARTED
    host.release_held_runner()


# ===========================================================================
# F. CRASH MATRIX
# ===========================================================================


def test_crash_after_arming_before_submission_seals_and_releases(
    tmp_path: Path,
) -> None:
    """CASE 1: the write-ahead boundary exists, the host never submitted."""

    _, store, authority, _, _, _, job, guest, host, orchestrator = _armed_system(
        tmp_path
    )
    # Prepare, arm, then "crash" before the submission critical section.
    request = authority.package_update_mutation_request(job.job_id)
    host.prepare_exact_package_mutation(request)
    authority.arm_package_update_mutation(
        job.job_id,
        job_packages(job),
        prepared_evidence_digest=ARBITRARY_EVIDENCE_DIGEST,
    )

    recovered = orchestrator.recover_job_owned_mutation(job.job_id)

    assert recovered.status is MutationStageStatus.NOT_SUBMITTED
    assert guest.mutations == []
    released = store.package_update_job(job.job_id)
    assert released.status is PackageUpdateJobStatus.BLOCKED
    # The checkpoint NEVER regresses, and no rollback authority is fabricated
    # for a mutation that provably did not happen.
    assert released.checkpoint is PackageUpdateCheckpoint.MUTATION_MAY_HAVE_STARTED
    assert released.mutation_may_have_started_at is not None
    assert released.mutation_completed_at is None
    assert PackageUpdateEventType.MUTATION_BLOCKED_BEFORE_SUBMISSION in _events(
        store, job.job_id
    )
    with pytest.raises(AuthorityConflict, match="terminal"):
        authority.select_package_update_rollback_target(job.job_id, ())


def test_crash_before_the_response_arrives_never_resubmits(tmp_path: Path) -> None:
    """CASE: the host received and ran the request; the answer was lost."""

    _, store, authority, _, _, _, job, guest, host, orchestrator = _armed_system(
        tmp_path
    )
    host.drop_response.add("execute_exact_package_mutation")

    first = orchestrator.execute_job_owned_mutation(job.job_id)

    assert first.status is MutationStageStatus.UNCERTAIN
    assert guest.mutations == [1]
    host.drop_response.clear()

    recovered = orchestrator.recover_job_owned_mutation(job.job_id)

    # The recovery reattaches to the SAME operation and proves it from the
    # journal's own evidence. It never runs a second mutation.
    assert recovered.status is MutationStageStatus.COMPLETED
    assert guest.mutations == [1]
    assert (
        store.package_update_job(job.job_id).checkpoint
        is PackageUpdateCheckpoint.MUTATION_COMPLETED
    )


def test_terminal_success_before_the_completion_commit_completes_once(
    tmp_path: Path,
) -> None:
    """CASE 3: apt finished; the backend died before committing completion."""

    _, store, authority, _, _, _, job, guest, host, orchestrator = _armed_system(
        tmp_path
    )
    host.drop_response.add("execute_exact_package_mutation")
    orchestrator.execute_job_owned_mutation(job.job_id)
    host.drop_response.clear()
    assert (
        store.package_update_job(job.job_id).checkpoint
        is PackageUpdateCheckpoint.MUTATION_MAY_HAVE_STARTED
    )

    first = orchestrator.recover_job_owned_mutation(job.job_id)
    second = orchestrator.recover_job_owned_mutation(job.job_id)

    assert first.status is second.status is MutationStageStatus.COMPLETED
    assert guest.mutations == [1]
    completed = store.package_update_job(job.job_id)
    assert (
        _events(store, job.job_id).count(PackageUpdateEventType.MUTATION_COMPLETED)
        == 1
    )
    assert completed.mutation_completed_at is not None


def test_a_running_operation_is_reattached_never_resubmitted(
    tmp_path: Path,
) -> None:
    """CASE 2: restart while the mutation is running."""

    _, store, authority, _, _, _, job, guest, host, orchestrator = _armed_system(
        tmp_path
    )
    host.runner_mode = "hold"
    submitted = orchestrator.execute_job_owned_mutation(job.job_id)
    assert submitted.status is MutationStageStatus.RUNNING

    for _ in range(3):
        again = orchestrator.recover_job_owned_mutation(job.job_id)
        assert again.status is MutationStageStatus.RUNNING
    assert guest.mutations == []

    request = authority.package_update_mutation_request(job.job_id)
    host.finish_held_runner(request)
    finished = orchestrator.recover_job_owned_mutation(job.job_id)

    assert finished.status is MutationStageStatus.COMPLETED
    assert guest.mutations == [1]


def test_a_runner_killed_without_a_terminal_record_stays_uncertain(
    tmp_path: Path,
) -> None:
    """CASE 5: the host evidence is ambiguous, so the job stays fenced."""

    _, store, authority, _, _, _, job, guest, host, orchestrator = _armed_system(
        tmp_path
    )
    host.runner_mode = "die"

    result = orchestrator.execute_job_owned_mutation(job.job_id)

    assert result.status is MutationStageStatus.UNCERTAIN
    fenced = store.package_update_job(job.job_id)
    assert fenced.status is PackageUpdateJobStatus.ACTIVE
    assert fenced.checkpoint is PackageUpdateCheckpoint.MUTATION_MAY_HAVE_STARTED

    # Every later attempt observes the same durable uncertainty and never
    # resubmits, whatever the guest looks like now.
    for _ in range(3):
        again = orchestrator.recover_job_owned_mutation(job.job_id)
        assert again.status is MutationStageStatus.UNCERTAIN
    assert guest.mutations == []


def test_a_failed_package_command_retains_ownership_and_rollback_authority(
    tmp_path: Path,
) -> None:
    _, store, authority, _, _, _, job, guest, host, orchestrator = _armed_system(
        tmp_path
    )
    guest.mutation_exit_code = 100
    guest.mutation_applies = False

    result = orchestrator.execute_job_owned_mutation(job.job_id)

    assert result.status is MutationStageStatus.TERMINAL_FAILURE
    assert guest.mutations == [1]
    fenced = store.package_update_job(job.job_id)
    assert fenced.status is PackageUpdateJobStatus.ACTIVE
    assert fenced.terminalized_at is None
    assert fenced.checkpoint is PackageUpdateCheckpoint.MUTATION_MAY_HAVE_STARTED
    assert PackageUpdateEventType.MUTATION_TERMINAL_FAILURE in _events(
        store, job.job_id
    )
    # The one global destructive slot is deliberately NOT released.
    from tests.test_package_update_job_authority import _add_approved_resource

    other_resource, _, other_approval = _add_approved_resource(store, authority)
    with pytest.raises(AuthorityConflict, match="global slot"):
        _issue(authority, other_resource, other_approval)
    # And the job's own snapshot remains its rollback target.
    identity = authority.package_update_snapshot_identity(job.job_id)
    ownership = authority.package_update_snapshot_ownership(job.job_id)
    target = authority.select_package_update_rollback_target(
        job.job_id, _canonical(ownership, identity)
    )
    assert target.snapshot_name == identity.snapshot_name


def test_a_delayed_request_can_never_mutate_after_a_recovery_seal(
    tmp_path: Path,
) -> None:
    """CASE K: the seal wins the lease; the delayed request must obey it."""

    _, store, authority, _, _, _, job, guest, host, orchestrator = _armed_system(
        tmp_path
    )
    request = authority.package_update_mutation_request(job.job_id)
    prepared = host.prepare_exact_package_mutation(request)
    authority.arm_package_update_mutation(
        job.job_id,
        job_packages(job),
        prepared_evidence_digest=ARBITRARY_EVIDENCE_DIGEST,
    )

    # Recovery seals the operation first.
    sealed = orchestrator.recover_job_owned_mutation(job.job_id)
    assert sealed.status is MutationStageStatus.NOT_SUBMITTED

    # The delayed original request finally reaches the host.
    late = host.execute_exact_package_mutation(
        request, prepared_evidence_digest=prepared.prepared_evidence_digest
    )

    assert late.state is HostMutationState.SEALED_NOT_SUBMITTED
    assert guest.mutations == []


def test_startup_recovery_never_frees_an_armed_job(tmp_path: Path) -> None:
    """CASE O: ordinary restart recovery leaves the mutation window alone."""

    _, store, authority, _, _, _, job, guest, host, orchestrator = _armed_system(
        tmp_path
    )
    host.runner_mode = "hold"
    orchestrator.execute_job_owned_mutation(job.job_id)

    assert authority.recover_interrupted_package_update_jobs() == ()
    preserved = store.package_update_job(job.job_id)
    assert preserved.status is PackageUpdateJobStatus.ACTIVE
    assert preserved.checkpoint is PackageUpdateCheckpoint.MUTATION_MAY_HAVE_STARTED
    host.release_held_runner()


def test_startup_recovery_never_frees_a_completed_mutation(tmp_path: Path) -> None:
    _, store, authority, _, _, _, job, guest, host, orchestrator = _armed_system(
        tmp_path
    )
    orchestrator.execute_job_owned_mutation(job.job_id)

    assert authority.recover_interrupted_package_update_jobs() == ()
    preserved = store.package_update_job(job.job_id)
    assert preserved.status is PackageUpdateJobStatus.ACTIVE
    assert preserved.checkpoint is PackageUpdateCheckpoint.MUTATION_COMPLETED


def test_two_concurrent_orchestrations_cause_exactly_one_mutation(
    tmp_path: Path,
) -> None:
    """CASE N: another attempt races the same job."""

    _, store, authority, _, _, _, job, guest, host, orchestrator = _armed_system(
        tmp_path
    )
    second = _orchestrator(authority, host)

    first_result = orchestrator.execute_job_owned_mutation(job.job_id)
    second_result = second.execute_job_owned_mutation(job.job_id)

    assert first_result.status is MutationStageStatus.COMPLETED
    assert second_result.status is MutationStageStatus.COMPLETED
    assert guest.mutations == [1]


def test_a_recovery_invocation_never_submits_even_with_a_clean_host(
    tmp_path: Path,
) -> None:
    """Only the invocation that proved and armed may submit."""

    _, store, authority, _, _, _, job, guest, host, orchestrator = _armed_system(
        tmp_path
    )
    authority.arm_package_update_mutation(
        job.job_id,
        job_packages(job),
        prepared_evidence_digest=ARBITRARY_EVIDENCE_DIGEST,
    )
    # The host journal is completely absent: nothing was ever prepared.
    result = orchestrator.recover_job_owned_mutation(job.job_id)

    assert result.status is MutationStageStatus.NOT_SUBMITTED
    assert guest.mutations == []
    assert "execute_exact_package_mutation" not in host.calls


# ===========================================================================
# G. SQL INVARIANTS
# ===========================================================================


def _assert_sql_rejected(store, statement, parameters):
    with pytest.raises(sqlite3.DatabaseError):
        with store._transaction() as connection:
            connection.execute(statement, parameters)


def test_sql_refuses_a_mutation_timestamp_without_its_checkpoint(
    tmp_path: Path,
) -> None:
    _, store, authority, _, _, _, job, *_ = _armed_system(tmp_path)
    _assert_sql_rejected(
        store,
        "UPDATE package_update_jobs SET mutation_may_have_started_at=? WHERE job_id=?",
        ("2026-01-01T00:00:00+00:00", job.job_id),
    )
    _assert_sql_rejected(
        store,
        "UPDATE package_update_jobs SET mutation_completed_at=? WHERE job_id=?",
        ("2026-01-01T00:00:00+00:00", job.job_id),
    )


def test_sql_refuses_the_mutation_checkpoint_without_its_identity(
    tmp_path: Path,
) -> None:
    _, store, authority, _, _, _, job, *_ = _armed_system(tmp_path)
    _assert_sql_rejected(
        store,
        "UPDATE package_update_jobs SET checkpoint='mutation_may_have_started' "
        "WHERE job_id=?",
        (job.job_id,),
    )


def test_sql_refuses_completion_before_the_uncertainty_boundary(
    tmp_path: Path,
) -> None:
    _, store, authority, _, _, _, job, *_ = _armed_system(tmp_path)
    _assert_sql_rejected(
        store,
        "UPDATE package_update_jobs SET checkpoint='mutation_completed', "
        "mutation_completed_at=? WHERE job_id=?",
        ("2026-01-01T00:00:00+00:00", job.job_id),
    )


def test_sql_refuses_health_started_without_proven_mutation_completion(
    tmp_path: Path,
) -> None:
    """Finding A: health validation may never start before the exact package
    mutation was independently proven complete.

    A job stuck at ``mutation_may_have_started`` with no completion proof --
    failed, partial, timed out, or simply unproven -- is exactly the job
    schema v14 keeps eligible for rollback (ranks 8/9) WITHOUT ever
    fabricating ``mutation_completed_at``. That rollback exception must never
    widen into a way for `health_started` (rank 7) to be reached the same
    way: unlike rollback, health is never compensation for an uncertain
    mutation, it is the next stage of one that already succeeded.
    """

    _, store, authority, _, _, _, job, *_ = _armed_system(tmp_path)
    authority.arm_package_update_mutation(
        job.job_id, job_packages(job), prepared_evidence_digest=ARBITRARY_EVIDENCE_DIGEST
    )
    armed = authority.package_update_job(job.job_id)
    assert armed.checkpoint is PackageUpdateCheckpoint.MUTATION_MAY_HAVE_STARTED
    assert armed.mutation_completed_at is None

    # The witness: a buggy backend statement tries to advance straight to
    # health_started while leaving mutation_completed_at NULL.
    _assert_sql_rejected(
        store,
        "UPDATE package_update_jobs SET checkpoint='health_started' WHERE job_id=?",
        (job.job_id,),
    )


def test_sql_still_permits_the_rollback_boundary_without_mutation_completion(
    tmp_path: Path,
) -> None:
    """The positive control for the fix above: the ranks 8/9 rollback
    exception schema v14 exists for must remain reachable from an unproven
    mutation, completely unaffected by the new health_started gate."""

    _, store, authority, _, _, _, job, *_ = _armed_system(tmp_path)
    authority.arm_package_update_mutation(
        job.job_id, job_packages(job), prepared_evidence_digest=ARBITRARY_EVIDENCE_DIGEST
    )
    armed = authority.package_update_job(job.job_id)
    assert armed.checkpoint is PackageUpdateCheckpoint.MUTATION_MAY_HAVE_STARTED
    assert armed.mutation_completed_at is None

    rollback_operation_id = str(uuid.uuid4())
    with store._transaction() as connection:
        connection.execute(
            "UPDATE package_update_jobs SET checkpoint='rollback_may_have_started', "
            "rollback_operation_id=?, rollback_may_have_started_at=? "
            "WHERE job_id=?",
            (rollback_operation_id, "2026-01-01T00:00:00+00:00", job.job_id),
        )

    after = authority.package_update_job(job.job_id)
    assert after.checkpoint is PackageUpdateCheckpoint.ROLLBACK_MAY_HAVE_STARTED
    assert after.mutation_completed_at is None


def test_sql_still_permits_health_started_after_a_proven_mutation(
    tmp_path: Path,
) -> None:
    """The other positive control: a mutation actually proven complete may
    still legally reach health_started under the new, narrower gate.

    Schema v16 additionally binds ``health_started_at`` to that checkpoint in
    both directions, so the legal write is the checkpoint AND its timestamp
    together -- which is exactly what
    ``InventoryAuthority.start_package_update_health`` commits in one
    statement.
    """

    _, store, authority, _, _, _, job, guest, host, orchestrator = _armed_system(
        tmp_path
    )
    orchestrator.execute_job_owned_mutation(job.job_id)
    completed = authority.package_update_job(job.job_id)
    assert completed.checkpoint is PackageUpdateCheckpoint.MUTATION_COMPLETED
    assert completed.mutation_completed_at is not None

    with store._transaction() as connection:
        connection.execute(
            "UPDATE package_update_jobs SET checkpoint='health_started', "
            "health_started_at=? WHERE job_id=?",
            ("2027-01-01T00:00:00+00:00", job.job_id),
        )

    after = authority.package_update_job(job.job_id)
    assert after.checkpoint is PackageUpdateCheckpoint.HEALTH_STARTED
    assert after.mutation_completed_at is not None
    assert after.health_started_at == "2027-01-01T00:00:00+00:00"


def test_sql_makes_the_mutation_identity_and_timestamps_write_once(
    tmp_path: Path,
) -> None:
    _, store, authority, _, _, _, job, guest, host, orchestrator = _armed_system(
        tmp_path
    )
    orchestrator.execute_job_owned_mutation(job.job_id)
    _assert_sql_rejected(
        store,
        "UPDATE package_update_jobs SET mutation_operation_id=? WHERE job_id=?",
        (str(uuid.uuid4()), job.job_id),
    )
    _assert_sql_rejected(
        store,
        "UPDATE package_update_jobs SET mutation_may_have_started_at=? WHERE job_id=?",
        ("2027-01-01T00:00:00+00:00", job.job_id),
    )
    _assert_sql_rejected(
        store,
        "UPDATE package_update_jobs SET mutation_completed_at=? WHERE job_id=?",
        ("2027-01-01T00:00:00+00:00", job.job_id),
    )


def test_sql_refuses_a_checkpoint_regression_out_of_the_mutation_window(
    tmp_path: Path,
) -> None:
    _, store, authority, _, _, _, job, *_ = _armed_system(tmp_path)
    authority.arm_package_update_mutation(
        job.job_id,
        job_packages(job),
        prepared_evidence_digest=ARBITRARY_EVIDENCE_DIGEST,
    )
    _assert_sql_rejected(
        store,
        "UPDATE package_update_jobs SET checkpoint='snapshot_confirmed' WHERE job_id=?",
        (job.job_id,),
    )


def test_two_jobs_can_never_share_one_mutation_operation_identity(
    tmp_path: Path,
) -> None:
    _, store, authority, _, _, _, job, *_ = _armed_system(tmp_path)
    authority.arm_package_update_mutation(
        job.job_id,
        job_packages(job),
        prepared_evidence_digest=ARBITRARY_EVIDENCE_DIGEST,
    )
    armed = store.package_update_job(job.job_id)
    with pytest.raises(sqlite3.DatabaseError):
        with store._transaction() as connection:
            connection.execute(
                "INSERT INTO package_update_jobs(job_id, mutation_operation_id) "
                "VALUES(?, ?)",
                (str(uuid.uuid4()), armed.mutation_operation_id),
            )


# ===========================================================================
# H. THE PURE COMPLETION PROOF
# ===========================================================================


def _material(rows):
    return frozenset(rows)


def test_the_completion_proof_holds_only_for_an_exact_landing() -> None:
    frozen = _material(
        {("apt", "amd64", "2.6.1", "2.6.2"), ("zlib1g", "amd64", "1.0", "1.1")}
    )
    pre = {("apt", "amd64"): "2.6.1", ("zlib1g", "amd64"): "1.0", ("bash", "amd64"): "5"}
    post = {("apt", "amd64"): "2.6.2", ("zlib1g", "amd64"): "1.1", ("bash", "amd64"): "5"}
    proof = prove_package_mutation_completion(
        frozen_material=frozen,
        post_state=PackageMutationPostState(
            pre_installed=pre, post_installed=post, post_unfinished=()
        ),
    )
    assert proof.proven is True
    assert proof.proved_material == frozen


@pytest.mark.parametrize(
    ("post", "unfinished", "expected"),
    [
        ({"apt": "2.6.3"}, (), "exact approved candidate"),
        ({"apt": "2.6.1"}, (), "exact approved candidate"),
        ({"apt": "2.6.2"}, (("x", "amd64", "half-configured"),), "unfinished"),
    ],
)
def test_the_completion_proof_refuses_every_inexact_landing(
    post, unfinished, expected
) -> None:
    frozen = _material({("apt", "amd64", "2.6.1", "2.6.2")})
    proof = prove_package_mutation_completion(
        frozen_material=frozen,
        post_state=PackageMutationPostState(
            pre_installed={("apt", "amd64"): "2.6.1"},
            post_installed={("apt", "amd64"): post["apt"]},
            post_unfinished=unfinished,
        ),
    )
    assert proof.proven is False
    assert expected in (proof.reason or "")


def test_the_completion_proof_refuses_a_wrong_starting_version() -> None:
    frozen = _material({("apt", "amd64", "2.6.1", "2.6.2")})
    proof = prove_package_mutation_completion(
        frozen_material=frozen,
        post_state=PackageMutationPostState(
            pre_installed={("apt", "amd64"): "2.6.0"},
            post_installed={("apt", "amd64"): "2.6.2"},
            post_unfinished=(),
        ),
    )
    assert proof.proven is False
    assert "approved pre-update version" in (proof.reason or "")


def test_completion_refuses_a_proof_over_another_jobs_material(
    tmp_path: Path,
) -> None:
    _, store, authority, _, _, _, job, guest, host, orchestrator = _armed_system(
        tmp_path
    )
    authority.arm_package_update_mutation(
        job.job_id,
        job_packages(job),
        prepared_evidence_digest=ARBITRARY_EVIDENCE_DIGEST,
    )
    # Post-state that lands some other plan entirely.
    with pytest.raises(AuthorityConflict, match="completion proof did not hold"):
        authority.complete_package_update_mutation(
            job.job_id,
            PackageMutationPostState(
                pre_installed={("other", "amd64"): "1"},
                post_installed={("other", "amd64"): "2"},
                post_unfinished=(),
            ),
        )
    assert (
        store.package_update_job(job.job_id).mutation_completed_at is None
    )


# ===========================================================================
# I. LOCK ORDER AND CONTENTION POLICY
# ===========================================================================


def test_the_submission_critical_section_never_wraps_the_package_command(
    tmp_path: Path,
) -> None:
    """No SQLite writer transaction may stay open while apt runs.

    The submission callback is invoked while the writer lock is held, so it
    must return the instant the host has journaled `submitted`. This proves
    the guest's own package command is never executed inside it.
    """

    _, store, authority, _, _, _, job, guest, host, orchestrator = _armed_system(
        tmp_path
    )
    host.runner_mode = "hold"
    inside: list[str] = []
    original = authority.execute_package_mutation_submission_if_current

    def _traced(job_id, submit, *, prepared_evidence_digest):
        def _wrapped():
            before = len(guest.mutations)
            result = submit()
            inside.append("returned")
            assert len(guest.mutations) == before, (
                "the package command ran inside the writer critical section"
            )
            return result

        return original(
            job_id, _wrapped, prepared_evidence_digest=prepared_evidence_digest
        )

    authority.execute_package_mutation_submission_if_current = _traced
    result = orchestrator.execute_job_owned_mutation(job.job_id)

    assert inside == ["returned"]
    assert result.status is MutationStageStatus.RUNNING
    host.release_held_runner()


def test_polling_a_running_mutation_holds_no_writer_lock(tmp_path: Path) -> None:
    """Host inspection must never happen inside an authority transaction.

    A mutation can legitimately run for many minutes. If the orchestrator
    held the authority store's one writer lock while polling it, every
    ordinary writer -- discovery, a package scan, an approval -- would be
    blocked for that whole time. This proves an unrelated writer can take
    `BEGIN IMMEDIATE` immediately, with a short timeout, during a poll.
    """

    _, store, authority, _, _, _, job, guest, host, orchestrator = _armed_system(
        tmp_path
    )
    host.runner_mode = "hold"
    observed: list[str] = []
    original = host._call

    def _probe(request, operation, **kwargs):
        if operation == "inspect_package_mutation_state":
            connection = sqlite3.connect(store.path, timeout=0.2)
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.rollback()
                observed.append("writer available")
            finally:
                connection.close()
        return original(request, operation, **kwargs)

    host._call = _probe
    result = orchestrator.execute_job_owned_mutation(job.job_id)

    assert result.status is MutationStageStatus.RUNNING
    assert observed, "the running mutation was never polled"
    assert set(observed) == {"writer available"}
    host.release_held_runner()


def test_the_mutation_submission_timeout_ceiling_fits_the_writer_budget() -> None:
    from app.inventory import contention_policy as policy

    assert (
        policy.AUTHORITY_WRITER_WAIT_BUDGET_SECONDS
        > policy.MAX_PACKAGE_MUTATION_CRITICAL_SECTION_SECONDS
    )
    assert (
        policy.MAX_PACKAGE_MUTATION_CRITICAL_SECTION_SECONDS
        == policy.MAX_PACKAGE_MUTATION_SUBMISSION_TIMEOUT_SECONDS
        + policy.BOUNDED_PROCESS_CLEANUP_SECONDS
    )


def test_the_transport_refuses_a_submission_timeout_above_the_ceiling(
    tmp_path: Path,
) -> None:
    from app.inventory import contention_policy as policy
    from app.package_update_mutation_host_control import (
        SshPackageUpdateMutationHostControl,
    )

    def _build(submission_timeout: int):
        return SshPackageUpdateMutationHostControl(
            host="pve.example",
            port=22,
            user="hubinet",
            private_key_path=Path("/etc/hubinet/key"),
            known_hosts_path=Path("/etc/hubinet/known_hosts"),
            timeout_seconds=600,
            submission_timeout_seconds=submission_timeout,
            max_result_bytes=8 * 1024 * 1024,
        )

    _build(policy.MAX_PACKAGE_MUTATION_SUBMISSION_TIMEOUT_SECONDS)
    with pytest.raises(ValueError, match="submission timeout"):
        _build(policy.MAX_PACKAGE_MUTATION_SUBMISSION_TIMEOUT_SECONDS + 1)


# ===========================================================================
# J. HOST EVIDENCE HANDLING
# ===========================================================================


def test_a_host_answering_about_another_operation_is_never_a_release(
    tmp_path: Path,
) -> None:
    _, store, authority, _, _, _, job, guest, host, orchestrator = _armed_system(
        tmp_path
    )
    authority.arm_package_update_mutation(
        job.job_id,
        job_packages(job),
        prepared_evidence_digest=ARBITRARY_EVIDENCE_DIGEST,
    )

    class WrongOperation:
        def inspect_package_mutation_state(self, request):
            return HostMutationResult(
                mutation_operation_id=str(uuid.uuid4()),
                state=HostMutationState.SEALED_NOT_SUBMITTED,
            )

    confused = _orchestrator(authority, WrongOperation())
    result = confused.recover_job_owned_mutation(job.job_id)

    assert result.status is MutationStageStatus.UNCERTAIN
    assert (
        store.package_update_job(job.job_id).status is PackageUpdateJobStatus.ACTIVE
    )


def test_an_unreadable_host_is_never_a_release(tmp_path: Path) -> None:
    _, store, authority, _, _, _, job, guest, host, orchestrator = _armed_system(
        tmp_path
    )
    authority.arm_package_update_mutation(
        job.job_id,
        job_packages(job),
        prepared_evidence_digest=ARBITRARY_EVIDENCE_DIGEST,
    )
    host.fail_operation["inspect_package_mutation_state"] = TimeoutError("gone")

    result = orchestrator.recover_job_owned_mutation(job.job_id)

    assert result.status is MutationStageStatus.UNCERTAIN
    assert (
        store.package_update_job(job.job_id).status is PackageUpdateJobStatus.ACTIVE
    )
    assert guest.mutations == []


def test_a_submission_that_wins_the_writer_lock_defeats_a_racing_seal(
    tmp_path: Path,
) -> None:
    """The release path and the submission path share one writer lock.

    Both critical sections take the authority store's single writer lock, so
    they can never interleave: whichever gets it first reaches the host
    first. This drives the seal transaction's own in-transaction seam to
    prove the block cannot be committed once the host has already crossed
    submission -- the check-then-commit race, mirrored onto the release path.
    """

    _, store, authority, _, _, _, job, guest, host, orchestrator = _armed_system(
        tmp_path
    )
    request = authority.package_update_mutation_request(job.job_id)
    prepared = host.prepare_exact_package_mutation(request)
    authority.arm_package_update_mutation(
        job.job_id,
        job_packages(job),
        prepared_evidence_digest=ARBITRARY_EVIDENCE_DIGEST,
    )

    def _submit_inside_the_seal(connection, *, job_id):
        # A concurrent, authorized submission crossing the host boundary
        # after the seal was attempted. It can only win if it took the host
        # lease first, so the seal must observe the post-submission phase.
        host.execute_exact_package_mutation(
            request,
            prepared_evidence_digest=prepared.prepared_evidence_digest,
        )

    seals: list[str] = []

    def _seal_then_submit():
        # Model the seal LOSING the host lease race: submission already
        # journaled `submitted` before the seal read the journal.
        _submit_inside_the_seal(None, job_id=job.job_id)
        fresh = host.seal_mutation_never_submitted(request)
        seals.append(fresh.state.value)
        return fresh.state, "seal attempted", fresh

    blocked, fresh = authority.resolve_pre_mutation_block(
        job.job_id, _seal_then_submit
    )

    assert blocked is False
    assert seals == ["terminal_success"]
    assert guest.mutations == [1]
    still_owned = store.package_update_job(job.job_id)
    assert still_owned.status is PackageUpdateJobStatus.ACTIVE
    assert still_owned.checkpoint is PackageUpdateCheckpoint.MUTATION_MAY_HAVE_STARTED


def test_a_failed_seal_never_releases_the_job(tmp_path: Path) -> None:
    _, store, authority, _, _, _, job, guest, host, orchestrator = _armed_system(
        tmp_path
    )
    request = authority.package_update_mutation_request(job.job_id)
    host.prepare_exact_package_mutation(request)
    authority.arm_package_update_mutation(
        job.job_id,
        job_packages(job),
        prepared_evidence_digest=ARBITRARY_EVIDENCE_DIGEST,
    )
    host.fail_operation["seal_mutation_never_submitted"] = RuntimeError("no answer")

    result = orchestrator.recover_job_owned_mutation(job.job_id)

    assert result.status is MutationStageStatus.UNCERTAIN
    fenced = store.package_update_job(job.job_id)
    assert fenced.status is PackageUpdateJobStatus.ACTIVE
    assert fenced.checkpoint is PackageUpdateCheckpoint.MUTATION_MAY_HAVE_STARTED


# ===========================================================================
# J. CONCURRENT PREPARE, AND THE ONE ACCEPTED EVIDENCE DIGEST
#
# Two invocations can both observe ACTIVE @ snapshot_confirmed, both prepare
# fresh evidence, and both derive the SAME deterministic operation identity.
# Identity therefore cannot be what decides who may submit. Exactly one
# evidence digest becomes authority-accepted, and only the invocation
# carrying it can cause a real package command.
# ===========================================================================


def test_exactly_one_evidence_digest_can_ever_become_authority_accepted(
    tmp_path: Path,
) -> None:
    """A real interleaving, not two sequential calls.

    Both threads are released from one barrier INSIDE the arming window, so
    they genuinely contend for the write-ahead transition.
    """

    _, store, authority, _, _, _, job, _, _, _ = _armed_system(tmp_path)
    digests = ["1" * 64, "2" * 64]
    barrier = threading.Barrier(len(digests))
    outcomes: list[tuple] = []
    lock = threading.Lock()

    def _arm(digest: str) -> None:
        barrier.wait(timeout=10)
        try:
            result = authority.arm_package_update_mutation(
                job.job_id, job_packages(job), prepared_evidence_digest=digest
            )
        except Exception as exc:  # noqa: BLE001 - the loser's failure is data
            result = exc
        with lock:
            outcomes.append((digest, result))

    threads = [threading.Thread(target=_arm, args=(digest,)) for digest in digests]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    armed = store.package_update_job(job.job_id)
    assert armed.checkpoint is PackageUpdateCheckpoint.MUTATION_MAY_HAVE_STARTED
    assert armed.accepted_prepared_evidence_digest in digests

    winners = [
        digest
        for digest, result in outcomes
        if not isinstance(result, Exception)
        and result[1] is PackageMutationArmOutcome.ARMED_NOW
    ]
    assert winners == [armed.accepted_prepared_evidence_digest]
    # Exactly one write-ahead boundary event, no matter who raced.
    assert (
        _events(store, job.job_id).count(
            PackageUpdateEventType.MUTATION_MAY_HAVE_STARTED
        )
        == 1
    )


def test_only_the_accepted_digest_reaches_the_host_submission(
    tmp_path: Path,
) -> None:
    """The loser's submission is refused BEFORE the callback.

    And it is refused with a narrow type that is deliberately NOT the
    seal-eligible one: the invocation whose evidence WAS accepted may be
    submitting a real package command at this very moment, so claiming "never
    submitted" on its behalf would be false.
    """

    _, store, authority, _, _, _, job, guest, host, _ = _armed_system(tmp_path)
    authority.arm_package_update_mutation(
        job.job_id,
        job_packages(job),
        prepared_evidence_digest=ARBITRARY_EVIDENCE_DIGEST,
    )
    callbacks: list[str] = []

    with pytest.raises(PackageMutationEvidenceNotAccepted):
        authority.execute_package_mutation_submission_if_current(
            job.job_id,
            lambda: callbacks.append("submitted"),
            prepared_evidence_digest=OTHER_EVIDENCE_DIGEST,
        )

    assert callbacks == []
    assert guest.mutations == []
    assert "execute_exact_package_mutation" not in host.calls
    # Still armed, still fenced, still owning the global slot.
    unchanged = store.package_update_job(job.job_id)
    assert unchanged.status is PackageUpdateJobStatus.ACTIVE
    assert unchanged.checkpoint is PackageUpdateCheckpoint.MUTATION_MAY_HAVE_STARTED
    assert unchanged.accepted_prepared_evidence_digest == ARBITRARY_EVIDENCE_DIGEST

    # The accepted digest still works: the refusal fenced nothing permanently.
    authority.execute_package_mutation_submission_if_current(
        job.job_id,
        lambda: callbacks.append("submitted"),
        prepared_evidence_digest=ARBITRARY_EVIDENCE_DIGEST,
    )
    assert callbacks == ["submitted"]


def test_an_invocation_that_lost_the_arming_race_becomes_recovery_only(
    tmp_path: Path,
) -> None:
    """Deriving the same operation identity is not permission to submit."""

    _, store, authority, _, _, _, job, guest, host, orchestrator = _armed_system(
        tmp_path
    )
    original = authority.arm_package_update_mutation

    def _lose_the_race(job_id, fresh_packages, *, prepared_evidence_digest):
        # Another invocation commits the boundary with ITS evidence first.
        original(
            job_id,
            fresh_packages,
            prepared_evidence_digest=ARBITRARY_EVIDENCE_DIGEST,
        )
        return original(
            job_id, fresh_packages, prepared_evidence_digest=prepared_evidence_digest
        )

    authority.arm_package_update_mutation = _lose_the_race

    result = orchestrator.execute_job_owned_mutation(job.job_id)

    assert guest.mutations == []
    assert "execute_exact_package_mutation" not in host.calls
    assert result.status is not MutationStageStatus.COMPLETED
    assert (
        store.package_update_job(job.job_id).accepted_prepared_evidence_digest
        == ARBITRARY_EVIDENCE_DIGEST
    )


def test_the_accepted_evidence_digest_is_write_once_in_sql(tmp_path: Path) -> None:
    _, store, authority, _, _, _, job, _, _, _ = _armed_system(tmp_path)
    authority.arm_package_update_mutation(
        job.job_id,
        job_packages(job),
        prepared_evidence_digest=ARBITRARY_EVIDENCE_DIGEST,
    )

    with pytest.raises(sqlite3.IntegrityError, match="write-once"):
        with store._transaction() as connection:
            connection.execute(
                "UPDATE package_update_jobs "
                "SET accepted_prepared_evidence_digest=? WHERE job_id=?",
                (OTHER_EVIDENCE_DIGEST, job.job_id),
            )

    assert (
        store.package_update_job(job.job_id).accepted_prepared_evidence_digest
        == ARBITRARY_EVIDENCE_DIGEST
    )


@pytest.mark.parametrize(
    "digest",
    ["", "z" * 64, "A" * 64, "a" * 63, "a" * 65],
)
def test_sql_refuses_a_malformed_accepted_evidence_digest(
    tmp_path: Path, digest
) -> None:
    _, store, authority, _, _, _, job, _, _, _ = _armed_system(tmp_path)

    with pytest.raises(sqlite3.IntegrityError):
        with store._transaction() as connection:
            connection.execute(
                "UPDATE package_update_jobs "
                "SET checkpoint='mutation_may_have_started', "
                "mutation_operation_id=?, mutation_may_have_started_at=?, "
                "accepted_prepared_evidence_digest=? WHERE job_id=?",
                (str(uuid.uuid4()), "2024-01-01T00:00:00+00:00", digest, job.job_id),
            )


def test_sql_refuses_an_armed_job_with_no_accepted_evidence_digest(
    tmp_path: Path,
) -> None:
    """The arming facts are one indivisible write-ahead authority fact."""

    _, store, authority, _, _, _, job, _, _, _ = _armed_system(tmp_path)

    with pytest.raises(sqlite3.IntegrityError):
        with store._transaction() as connection:
            connection.execute(
                "UPDATE package_update_jobs "
                "SET checkpoint='mutation_may_have_started', "
                "mutation_operation_id=?, mutation_may_have_started_at=? "
                "WHERE job_id=?",
                (str(uuid.uuid4()), "2024-01-01T00:00:00+00:00", job.job_id),
            )


def test_sql_refuses_a_completed_mutation_without_its_accepted_evidence(
    tmp_path: Path,
) -> None:
    _, store, authority, _, _, _, job, _, _, _ = _armed_system(tmp_path)

    with pytest.raises(sqlite3.IntegrityError):
        with store._transaction() as connection:
            connection.execute(
                "UPDATE package_update_jobs SET checkpoint='mutation_completed', "
                "mutation_operation_id=?, mutation_may_have_started_at=?, "
                "mutation_completed_at=? WHERE job_id=?",
                (
                    str(uuid.uuid4()),
                    "2024-01-01T00:00:00+00:00",
                    "2024-01-01T00:01:00+00:00",
                    job.job_id,
                ),
            )


def test_a_running_scan_retries_to_a_real_mutation_without_a_restart(
    tmp_path: Path,
) -> None:
    """The pre-arm liveness contract, end to end.

    `AUTHORITY_TEMPORARILY_UNAVAILABLE` advertises "retry later", so retrying
    later must actually work. Preparation happens BEFORE the write-ahead
    arming transaction and creates no durable host state, so an attempt that
    cannot arm for an ordinary transient leaves the job exactly where it was
    with nothing on the host to go stale -- and the next attempt is an
    ordinary fresh preparation, not a recovery.

    A backend PROCESS RESTART must never be required to get past a scan that
    happened to be running.
    """

    _, store, authority, resource, scan, _, job, guest, host, orchestrator = (
        _armed_system(tmp_path)
    )
    request = authority.package_update_mutation_request(job.job_id)
    running = authority.issue_package_scan(resource.resource_id)

    first = orchestrator.execute_job_owned_mutation(job.job_id)

    assert first.status is MutationStageStatus.AUTHORITY_TEMPORARILY_UNAVAILABLE
    assert "prepare_exact_package_mutation" in host.calls
    assert "execute_exact_package_mutation" not in host.calls
    assert guest.mutations == []
    # Nothing durable was created anywhere: not on the host, not in the job.
    assert host.journal.read(request.mutation_operation_id) is None
    waiting = store.package_update_job(job.job_id)
    assert waiting.status is PackageUpdateJobStatus.ACTIVE
    assert waiting.checkpoint is PackageUpdateCheckpoint.SNAPSHOT_CONFIRMED
    assert waiting.mutation_may_have_started_at is None
    assert waiting.accepted_prepared_evidence_digest is None

    # The scan completes with the same material, so the job stays current.
    authority.finalize_successful_package_scan(
        running.scan_run_id,
        os_id="debian",
        os_version="12",
        packages=scan.packages,
        reboot_required=None,
    )

    # The SAME orchestrator object, on the SAME process, simply tries again.
    second = orchestrator.execute_job_owned_mutation(job.job_id)

    assert second.status is MutationStageStatus.COMPLETED
    assert guest.mutations == [1]
    completed = store.package_update_job(job.job_id)
    assert completed.checkpoint is PackageUpdateCheckpoint.MUTATION_COMPLETED
    assert completed.accepted_prepared_evidence_digest is not None


def test_a_lost_prepare_response_is_retryable_without_a_restart(
    tmp_path: Path,
) -> None:
    """The host did all its read-only work; the answer never came back.

    Preparation is read-only in BOTH senses -- no package changes and no
    durable journal record -- so a lost response leaves the operation
    identity completely untouched. The next attempt is an ordinary fresh
    preparation against fresh evidence, never a permanent HOST_FAILURE.
    """

    _, store, authority, _, _, _, job, guest, host, orchestrator = _armed_system(
        tmp_path
    )
    request = authority.package_update_mutation_request(job.job_id)
    host.drop_response.add("prepare_exact_package_mutation")

    lost = orchestrator.execute_job_owned_mutation(job.job_id)

    assert lost.status is MutationStageStatus.HOST_FAILURE
    assert lost.failure_class is PackageScanFailure.TIMEOUT
    assert guest.mutations == []
    assert host.journal.read(request.mutation_operation_id) is None
    unchanged = store.package_update_job(job.job_id)
    assert unchanged.status is PackageUpdateJobStatus.ACTIVE
    assert unchanged.checkpoint is PackageUpdateCheckpoint.SNAPSHOT_CONFIRMED
    assert unchanged.mutation_may_have_started_at is None

    host.drop_response.clear()

    retried = orchestrator.execute_job_owned_mutation(job.job_id)

    assert retried.status is MutationStageStatus.COMPLETED
    assert guest.mutations == [1]


def test_a_crash_after_preparing_before_arming_strands_nothing(
    tmp_path: Path,
) -> None:
    """CASE: the backend prepared and then died before the arming transaction.

    No mutation authority ever existed and no host mutation state ever
    existed, so there is nothing to seal and nothing to recover. Both exits
    stay open: an ordinary retry proceeds normally, and ordinary startup
    recovery still interrupts a job sitting at `snapshot_confirmed` exactly
    as it always did.
    """

    _, store, authority, _, _, _, job, guest, host, orchestrator = _armed_system(
        tmp_path
    )
    request = authority.package_update_mutation_request(job.job_id)
    # A previous, now-dead invocation prepared this exact operation.
    prepared = host.prepare_exact_package_mutation(request)

    assert prepared.state is HostMutationState.ABSENT
    assert prepared.prepared_evidence_digest is not None
    assert host.journal.read(request.mutation_operation_id) is None
    orphaned = store.package_update_job(job.job_id)
    assert orphaned.status is PackageUpdateJobStatus.ACTIVE
    assert orphaned.checkpoint is PackageUpdateCheckpoint.SNAPSHOT_CONFIRMED
    assert orphaned.accepted_prepared_evidence_digest is None

    # Startup recovery behaviour is unchanged: `snapshot_confirmed` is a
    # startup-interruptible checkpoint, the confirmed snapshot is retained,
    # and no rollback authority is fabricated.
    assert authority.recover_interrupted_package_update_jobs() == (job.job_id,)
    recovered = store.package_update_job(job.job_id)
    assert recovered.status is PackageUpdateJobStatus.INTERRUPTED
    assert recovered.snapshot_confirmed_at is not None
    assert recovered.mutation_may_have_started_at is None
    assert guest.mutations == []


def test_a_dead_backends_preparation_never_blocks_the_next_attempt(
    tmp_path: Path,
) -> None:
    """The same crash, resolved the other way: by simply trying again.

    This is the case the pre-arm durable journal used to make unreachable.
    A dead backend's preparation left an immutable host `intent`, no later
    invocation could obtain its digest, and the job could not proceed until
    a restart interrupted it. It must now proceed on the next ordinary
    attempt.
    """

    _, store, authority, _, _, _, job, guest, host, orchestrator = _armed_system(
        tmp_path
    )
    request = authority.package_update_mutation_request(job.job_id)
    host.prepare_exact_package_mutation(request)

    result = orchestrator.execute_job_owned_mutation(job.job_id)

    assert result.status is MutationStageStatus.COMPLETED
    assert guest.mutations == [1]
    assert (
        store.package_update_job(job.job_id).checkpoint
        is PackageUpdateCheckpoint.MUTATION_COMPLETED
    )


def test_only_the_accepted_digest_ever_becomes_the_host_intent(
    tmp_path: Path,
) -> None:
    """Two real preparations, one accepted digest, one host intent.

    With preparation durable-free, both invocations legitimately produce
    evidence and both derive the same deterministic operation identity, so
    identity cannot be what decides who may mutate. The arming transaction
    picks the winner, the loser's submission is refused BEFORE the host is
    called at all, and the host journal's very first record is therefore
    created from the accepted digest and no other.
    """

    _, store, authority, _, _, _, job, guest, host, orchestrator = _armed_system(
        tmp_path
    )
    request = authority.package_update_mutation_request(job.job_id)

    losing = host.prepare_exact_package_mutation(request)
    # The guest moves on between the two readings, so the two digests differ
    # for a real reason rather than by construction.
    guest.installed[("tzdata", "all")] = "2023d-1"
    winning = host.prepare_exact_package_mutation(request)
    assert losing.state is HostMutationState.ABSENT
    assert winning.state is HostMutationState.ABSENT
    assert losing.prepared_evidence_digest != winning.prepared_evidence_digest
    # Neither preparation created anything to contend over.
    assert host.journal.read(request.mutation_operation_id) is None

    outcome, armed, _ = authority.arm_package_update_mutation(
        job.job_id,
        job_packages(job),
        prepared_evidence_digest=winning.prepared_evidence_digest,
    )
    assert armed is PackageMutationArmOutcome.ARMED_NOW
    assert (
        store.package_update_job(job.job_id).accepted_prepared_evidence_digest
        == winning.prepared_evidence_digest
    )

    # The loser tries to submit its own evidence. Authority refuses before
    # the callback, so the host is never asked and no intent is created.
    with pytest.raises(PackageMutationEvidenceNotAccepted):
        authority.execute_package_mutation_submission_if_current(
            job.job_id,
            lambda: host.execute_exact_package_mutation(
                request,
                prepared_evidence_digest=losing.prepared_evidence_digest,
            ),
            prepared_evidence_digest=losing.prepared_evidence_digest,
        )
    assert "execute_exact_package_mutation" not in host.calls
    assert host.journal.read(request.mutation_operation_id) is None
    assert guest.mutations == []

    # Only the winner reaches the host, and its digest becomes the binding.
    submitted = authority.execute_package_mutation_submission_if_current(
        job.job_id,
        lambda: host.execute_exact_package_mutation(
            request, prepared_evidence_digest=winning.prepared_evidence_digest
        ),
        prepared_evidence_digest=winning.prepared_evidence_digest,
    )
    assert submitted.state is HostMutationState.SUBMITTED
    assert guest.mutations == [1]

    # And the loser's delayed request can never mutate a second time.
    late = host.execute_exact_package_mutation(
        request, prepared_evidence_digest=losing.prepared_evidence_digest
    )
    assert late.state is HostMutationState.TERMINAL_SUCCESS
    assert guest.mutations == [1]
