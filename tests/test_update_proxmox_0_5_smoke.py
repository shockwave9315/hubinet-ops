"""Sandboxed smoke tests for deploy/update-proxmox-0.5.sh.

Executes the real update script as a subprocess against a synthetic
"already bootstrapped" installation (tests/_update_fake_pve.py) and the
same hermetic fake-command layer the bootstrap smoke suite uses
(tests/_bootstrap_fake_pve.py) -- no real pct/pveum/pveam/pvesh/pvesm/nft/
network/PVE/HA endpoint is ever contacted. Per AGENTS.md's deployment-
script sandbox boundary, this file only ever runs inside the Docker-based
ephemeral-CI/local-CI sandbox; every test is a hard skip elsewhere.
"""

from __future__ import annotations

import fcntl
import json
import re
import selectors
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap_fake_pve import git_head_sha  # noqa: E402
from _update_fake_pve import (  # noqa: E402
    FAKE_BACKEND_INSTANCE_ID,
    FAKE_RUN_ID,
    FAKE_VMID,
    UPDATE_BOUNDARY_HELPERS,
    UPDATE_BOUNDARY_JOURNAL_DIRS,
    boundary_helper_host_path,
    boundary_key_ct_path,
    build_update_target_checkout,
    seed_installed_environment,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
UPDATE_SCRIPT = REPO_ROOT / "deploy" / "update-proxmox-0.5.sh"

pytestmark = [
    pytest.mark.skipif(
        __import__("os").environ.get("HUBINET_OPS_SYSTEM_SANDBOX") != "1",
        reason=(
            "this file executes the real update script and per AGENTS.md's "
            "deployment-script sandbox boundary only ever runs inside the "
            "ephemeral-CI/local Docker sandbox"
        ),
    ),
    pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not available"),
]


def _run(env, args, *, timeout=30):
    argv = ["bash", str(UPDATE_SCRIPT), *args]
    result = subprocess.run(
        argv, cwd=str(REPO_ROOT), env=env, capture_output=True, timeout=timeout
    )
    result.stdout = result.stdout.decode("utf-8", errors="replace")
    result.stderr = result.stderr.decode("utf-8", errors="replace")
    return result


def _run_with_mutation_after_plan(env, args, mutation, *, timeout=30):
    """Run interactively, mutating only after the complete plan is visible."""
    interactive_args = [arg for arg in args if arg not in {"--non-interactive", "--yes"}]
    argv = ["bash", str(UPDATE_SCRIPT), *interactive_args]
    process = subprocess.Popen(
        argv,
        cwd=str(REPO_ROOT),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None

    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    chunks = {"stdout": [], "stderr": []}
    approved = False
    deadline = time.monotonic() + timeout
    plan_end = b"not required (backend_instance_id is preserved)\n"

    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                raise subprocess.TimeoutExpired(argv, timeout)
            for key, _ in selector.select(timeout=min(0.1, remaining)):
                chunk = key.fileobj.read1(65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                chunks[key.data].append(chunk)
                if not approved and plan_end in b"".join(chunks["stdout"]):
                    # update_plan_confirm cannot return until this write.
                    # Therefore classification and the displayed plan used
                    # the old facts, while fence revalidation sees the drift.
                    mutation()
                    process.stdin.write(b"y\n")
                    process.stdin.flush()
                    process.stdin.close()
                    approved = True
        returncode = process.wait(timeout=max(0.1, deadline - time.monotonic()))
    except BaseException:
        if process.poll() is None:
            process.kill()
            process.wait()
        raise
    finally:
        selector.close()

    assert approved, "the updater exited before displaying the complete approval plan"
    return subprocess.CompletedProcess(
        argv,
        returncode,
        b"".join(chunks["stdout"]).decode("utf-8", errors="replace"),
        b"".join(chunks["stderr"]).decode("utf-8", errors="replace"),
    )


def _base_args(target_dir, *, vmid=FAKE_VMID, expected_sha=None, extra=()):
    args = ["--vmid", vmid, "--source-dir", str(target_dir), "--non-interactive", "--yes"]
    if expected_sha is None:
        expected_sha = git_head_sha(target_dir)
    if expected_sha is not False:
        args += ["--expected-sha", expected_sha]
    return args + list(extra)


def _update_state_path(env, vmid, suffix):
    return (
        Path(env.env["HUBINET_OPS_TEST_HOST_ROOT"])
        / "var" / "lib" / "hubinet-ops" / "update-state"
        / f"vmid-{vmid}.{suffix}"
    )


def _journal_run_id(journal_text):
    for line in journal_text.splitlines():
        if line.startswith("run_id="):
            return line.split("=", 1)[1]
    raise AssertionError(f"journal carries no run_id: {journal_text!r}")


def _assert_recovery_repushed_only_the_authority_tool(env, before_recovery, run_id):
    """Startup recovery re-pushes exactly ONE thing: the run-owned authority
    helper, at the SAME reconstructed run-id path.

    That single push is recovery INFRASTRUCTURE -- the container's volatile
    /tmp may legitimately have been cleared by a PVE/CT restart, and every
    remaining recovery operation (three-valued path-state probes, the
    fail-closed authority remove/restore) runs through that helper. It is
    still never a new Phase U2 deployment plan, so nothing else may be
    pushed: not the pre-update HTTP probe, not a source tarball, not the
    venv-staging tool, not the acceptance script.
    """
    pushes = [line for line in env.log_lines()[before_recovery:] if line.startswith("pct push")]
    assert len(pushes) == 1, pushes
    source, destination = pushes[0].split()[-2:]
    assert source.endswith("/deploy/lib/hubinet-ops-authority-tool.py"), pushes
    assert destination == f"/tmp/hubinet-ops-authority-tool-{run_id}.py", pushes


def _assert_recovery_pushed_nothing(env, before_recovery):
    assert not any(
        line.startswith("pct push") for line in env.log_lines()[before_recovery:]
    ), "startup recovery must not enter normal planning and push a new tool"


@pytest.fixture
def target_checkout(tmp_path):
    return build_update_target_checkout(tmp_path / "target", REPO_ROOT, schema_version=10)


# ---------------------------------------------------------------------------
# A. Code-only update -- the most important long-term case. Must be boring.
# ---------------------------------------------------------------------------


class TestCodeOnlyUpdate:
    def test_boring_update_preserves_everything(self, tmp_path):
        env = seed_installed_environment(
            tmp_path, schema_version=10, installed_source_sha="1" * 40
        )
        pre_state = env.state()
        pre_authorized_keys = (
            Path(env.env["HUBINET_OPS_TEST_HOST_ROOT"]) / "root" / ".ssh" / "authorized_keys"
        ).read_text(encoding="utf-8")
        pre_helper = env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/.venv/bin/pip")
        pre_agent_env = env.ct_file_text(FAKE_VMID, "/etc/hubinet-ops/agent.env")
        pre_nft = env.ct_file_text(FAKE_VMID, "/etc/nftables.conf")

        target = build_update_target_checkout(tmp_path / "target-boring", REPO_ROOT, schema_version=10)
        result = _run(env.env, _base_args(target))
        assert result.returncode == 0, result.stderr

        # App payload replaced with the target's own marker content.
        assert (
            "Fake target store.py"
            in env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/app/inventory/store.py")
        )
        # requirements/unit/helper untouched -- unchanged content, no pip run.
        assert env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/.venv/bin/pip") == pre_helper
        assert "venv_create" not in "\n".join(env.log_lines())
        assert not any(" pip " in line or line.endswith("/bin/pip") for line in env.log_lines())
        # Credentials/config/firewall byte-identical.
        post_authorized_keys = (
            Path(env.env["HUBINET_OPS_TEST_HOST_ROOT"]) / "root" / ".ssh" / "authorized_keys"
        ).read_text(encoding="utf-8")
        assert post_authorized_keys == pre_authorized_keys
        assert env.ct_file_text(FAKE_VMID, "/etc/hubinet-ops/agent.env") == pre_agent_env
        assert env.ct_file_text(FAKE_VMID, "/etc/nftables.conf") == pre_nft
        # PVE identity unchanged.
        post_state = env.state()
        assert post_state["pve_users"] == pre_state["pve_users"]
        assert post_state["pve_tokens"] == pre_state["pve_tokens"]
        assert post_state["pve_roles"] == pre_state["pve_roles"]
        # Authority DB preserved (same backend_instance_id, schema unchanged).
        db = json.loads(env.ct_file_text(FAKE_VMID, "/var/lib/hubinet-ops/authority.db"))
        assert db["backend_instance_id"] == FAKE_BACKEND_INSTANCE_ID
        assert db["schema_version"] == 10
        # Installed-source marker recorded.
        marker = env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/.hubinet-source-commit").strip()
        assert marker == git_head_sha(target)
        assert "backend_instance_id" in result.stdout or "backend_instance_id" in result.stderr
        assert not list(
            env.ct_file(FAKE_VMID, "/opt/hubinet-ops").glob("requirements.txt.staged-*")
        )

    def test_failed_after_app_activation_never_stages_unchanged_requirements(self, tmp_path):
        env = seed_installed_environment(
            tmp_path,
            scenario_overrides={"discovery_result": "backend_unreachable"},
        )
        target = build_update_target_checkout(tmp_path / "target-failed-code-only", REPO_ROOT)
        result = _run(env.env, _base_args(target))

        assert result.returncode != 0
        assert "rollback complete" in result.stderr
        assert not list(
            env.ct_file(FAKE_VMID, "/opt/hubinet-ops").glob("requirements.txt.staged-*")
        )


# ---------------------------------------------------------------------------
# B. requirements.txt change -- new venv built at its final live path.
# ---------------------------------------------------------------------------


class TestRequirementsChanged:
    def test_changed_requirements_stage_and_swap_venv(self, tmp_path):
        env = seed_installed_environment(tmp_path, installed_requirements="fastapi==0.100.0\n")
        target = build_update_target_checkout(
            tmp_path / "target-reqs", REPO_ROOT, requirements_text="fastapi==0.116.1\n"
        )
        result = _run(env.env, _base_args(target))
        assert result.returncode == 0, result.stderr
        assert env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/requirements.txt") == "fastapi==0.116.1\n"

    def test_venv_build_tool_push_failure_leaves_old_service_untouched(self, tmp_path):
        """Phase U3 still fails harmlessly, before the mutation window.

        The venv is no longer BUILT during staging (see
        TestVenvBuiltAtFinalPathP1), but the small build helper is still
        pushed there -- deliberately, so a transport failure is discovered
        while the old service is untouched rather than after it is
        stopped.
        """
        env = seed_installed_environment(
            tmp_path,
            installed_requirements="fastapi==0.100.0\n",
            scenario_overrides={
                "pct_push_fail_dest_suffixes": ["hubinet-ops-update-venv-stage.py"]
            },
        )
        target = build_update_target_checkout(
            tmp_path / "target-reqs-push-fail", REPO_ROOT, requirements_text="fastapi==0.116.1\n"
        )
        result = _run(env.env, _base_args(target))
        assert result.returncode != 0
        assert "the ACTIVE virtualenv was never touched" in result.stderr
        assert env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/requirements.txt") == "fastapi==0.100.0\n"
        assert env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/.venv/bin/pip") == "#!/bin/sh\n"
        assert not any(
            "systemctl" in line and " stop " in line for line in env.log_lines()
        )


# ---------------------------------------------------------------------------
# C/D. Unit and helper changes, negative controls.
# ---------------------------------------------------------------------------


class TestUnitAndHelperChanged:
    def test_changed_unit_replaced_and_reloaded(self, tmp_path):
        env = seed_installed_environment(tmp_path)
        target = build_update_target_checkout(
            tmp_path / "target-unit", REPO_ROOT,
            unit_text="[Unit]\nDescription=changed\n",
        )
        result = _run(env.env, _base_args(target))
        assert result.returncode == 0, result.stderr
        assert "changed" in env.ct_file_text(FAKE_VMID, "/etc/systemd/system/hubinet-ops.service")
        assert any("daemon-reload" in line for line in env.log_lines())

    def test_changed_helper_replaces_content_at_same_path(self, tmp_path):
        env = seed_installed_environment(tmp_path)
        helper_path = Path(env.env["HUBINET_OPS_TEST_HOST_ROOT"]) / "usr" / "local" / "libexec" / f"hubinet-package-scan-helper-{FAKE_RUN_ID}"
        pre_authorized_keys = (
            Path(env.env["HUBINET_OPS_TEST_HOST_ROOT"]) / "root" / ".ssh" / "authorized_keys"
        ).read_text(encoding="utf-8")
        target = build_update_target_checkout(
            tmp_path / "target-helper", REPO_ROOT,
            helper_text="#!/usr/bin/env python3\n# changed helper\n",
        )
        result = _run(env.env, _base_args(target))
        assert result.returncode == 0, result.stderr
        assert helper_path.read_text(encoding="utf-8") == "#!/usr/bin/env python3\n# changed helper\n"
        post_authorized_keys = (
            Path(env.env["HUBINET_OPS_TEST_HOST_ROOT"]) / "root" / ".ssh" / "authorized_keys"
        ).read_text(encoding="utf-8")
        assert post_authorized_keys == pre_authorized_keys

    def test_unchanged_helper_is_never_touched(self, tmp_path):
        env = seed_installed_environment(tmp_path)
        helper_path = Path(env.env["HUBINET_OPS_TEST_HOST_ROOT"]) / "usr" / "local" / "libexec" / f"hubinet-package-scan-helper-{FAKE_RUN_ID}"
        original_mtime = helper_path.stat().st_mtime_ns
        target = build_update_target_checkout(tmp_path / "target-nochange", REPO_ROOT)
        result = _run(env.env, _base_args(target))
        assert result.returncode == 0, result.stderr
        assert helper_path.stat().st_mtime_ns == original_mtime


# ---------------------------------------------------------------------------
# E/F. Authority schema reset -- authorized vs. refused.
# ---------------------------------------------------------------------------


class TestAuthoritySchemaReset:
    def test_reset_authorized_backs_up_and_regenerates_identity(self, tmp_path):
        # A different discovery_backend_instance_id than the seeded
        # pre-update identity -- the fake's post-update acceptance
        # simulation is scenario-driven, so a real destructive reset
        # producing a NEW identity is simulated explicitly rather than
        # assumed; the updater itself must then prove the two differ.
        new_backend_instance_id = "11111111-1111-4111-8111-111111111111"
        env = seed_installed_environment(
            tmp_path, schema_version=9,
            scenario_overrides={"discovery_backend_instance_id": new_backend_instance_id},
        )
        target = build_update_target_checkout(tmp_path / "target-v10", REPO_ROOT, schema_version=10)
        result = _run(
            env.env,
            _base_args(target, extra=["--allow-authority-reset"]),
        )
        assert result.returncode == 0, result.stderr
        assert "reset-required" in result.stdout or "RESET" in result.stdout
        assert "re-enrollment" in result.stdout.lower() or "re-enrollment" in result.stderr.lower()
        db_path = env.ct_file(FAKE_VMID, "/var/lib/hubinet-ops/authority.db")
        assert not db_path.exists(), (
            "the old authority.db must be removed once its backup is validated -- "
            "the updater never writes schema DDL itself; the target runtime creates "
            "a fresh database on its own next start"
        )
        # The backup directory must exist and retain the OLD identity.
        backups_root = env.ct_file(FAKE_VMID, "/var/lib/hubinet-ops/update-backups")
        backup_files = list(backups_root.rglob("authority.db"))
        assert backup_files, "expected a retained authority DB backup"
        backup_data = json.loads(backup_files[0].read_text(encoding="utf-8"))
        assert backup_data["backend_instance_id"] == FAKE_BACKEND_INSTANCE_ID
        assert backup_data["schema_version"] == 9

    def test_reset_refused_without_allow_flag_makes_zero_mutation(self, tmp_path):
        env = seed_installed_environment(tmp_path, schema_version=9)
        target = build_update_target_checkout(tmp_path / "target-v10-refused", REPO_ROOT, schema_version=10)
        result = _run(env.env, _base_args(target))
        assert result.returncode != 0
        assert "--allow-authority-reset" in result.stderr
        db = json.loads(env.ct_file_text(FAKE_VMID, "/var/lib/hubinet-ops/authority.db"))
        assert db["schema_version"] == 9
        assert db["backend_instance_id"] == FAKE_BACKEND_INSTANCE_ID
        assert env.state()["vmids"][FAKE_VMID]["service"] == "active"
        assert not any(
            "systemctl" in line and "stop" in line for line in env.log_lines()
        )


# ---------------------------------------------------------------------------
# G. Target failure AFTER a destructive reset -- coherent rollback,
#    including the authority database.
# ---------------------------------------------------------------------------


class TestRollbackAfterDestructiveResetFailure:
    def test_full_rollback_restores_old_code_and_old_db(self, tmp_path):
        env = seed_installed_environment(
            tmp_path,
            schema_version=9,
            scenario_overrides={"discovery_result": "backend_unreachable"},
        )
        target = build_update_target_checkout(tmp_path / "target-v10-fail", REPO_ROOT, schema_version=10)
        result = _run(env.env, _base_args(target, extra=["--allow-authority-reset"]))
        assert result.returncode != 0

        db = json.loads(env.ct_file_text(FAKE_VMID, "/var/lib/hubinet-ops/authority.db"))
        assert db["schema_version"] == 9, "the OLD authority database must be restored, not a new schema-10 one"
        assert db["backend_instance_id"] == FAKE_BACKEND_INSTANCE_ID
        assert env.state()["vmids"][FAKE_VMID]["service"] == "active"
        # The original (pre-update) app payload has no app/inventory/
        # subtree at all -- only the target's staged payload does, so its
        # absence after rollback proves the OLD app was restored, not left
        # paired with a new/incompatible schema.
        assert not env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app/inventory/store.py").exists()


# ---------------------------------------------------------------------------
# I. Ownership mismatch -- fail closed before any mutation.
# ---------------------------------------------------------------------------


class TestOwnershipFailClosed:
    def test_wrong_run_id_marker_stops_before_mutation(self, tmp_path):
        env = seed_installed_environment(tmp_path)
        # Corrupt the CT-side public key comment so it no longer matches
        # the authorized_keys/PVE-identity run-id.
        pubkey = env.ct_file(FAKE_VMID, "/etc/hubinet-ops/host-control/id_ed25519.pub")
        pubkey.write_text(
            "ssh-ed25519 QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE= "
            f"hubinet-ops-package-scan-vmid-{FAKE_VMID}-deadbeefdeadbeefdeadbeefdeadbeef\n",
            encoding="utf-8",
        )
        target = build_update_target_checkout(tmp_path / "target-ownership", REPO_ROOT)
        result = _run(env.env, _base_args(target))
        assert result.returncode != 0
        assert "ownership verification failed" in result.stderr
        assert env.state()["vmids"][FAKE_VMID]["service"] == "active"

    def test_wrong_privilege_set_stops_before_mutation(self, tmp_path):
        env = seed_installed_environment(tmp_path)
        state = env.state()
        state["pve_roles"]["HubinetOpsR0Auditor"] = ["Sys.Audit", "VM.Audit", "VM.Console"]
        env.state_path.write_text(json.dumps(state), encoding="utf-8")
        target = build_update_target_checkout(tmp_path / "target-privs", REPO_ROOT)
        result = _run(env.env, _base_args(target))
        assert result.returncode != 0
        assert "verification failed" in result.stderr


# ---------------------------------------------------------------------------
# J. Target provenance -- fail closed, zero mutation.
# ---------------------------------------------------------------------------


class TestProvenanceFailClosed:
    def test_expected_sha_mismatch_stops_before_mutation(self, tmp_path):
        env = seed_installed_environment(tmp_path)
        target = build_update_target_checkout(tmp_path / "target-sha-mismatch", REPO_ROOT)
        result = _run(env.env, _base_args(target, expected_sha="f" * 40))
        assert result.returncode != 0
        assert "does not match --expected-sha" in result.stderr
        assert env.state()["vmids"][FAKE_VMID]["service"] == "active"

    def test_dirty_worktree_stops_before_mutation(self, tmp_path):
        env = seed_installed_environment(tmp_path)
        target = build_update_target_checkout(tmp_path / "target-dirty", REPO_ROOT)
        (target / "requirements.txt").write_text("dirty-change\n", encoding="utf-8")
        result = _run(env.env, _base_args(target, expected_sha=False))
        assert result.returncode != 0
        assert "dirty working tree" in result.stderr

    def test_non_interactive_without_expected_sha_stops(self, tmp_path):
        env = seed_installed_environment(tmp_path)
        target = build_update_target_checkout(tmp_path / "target-no-sha", REPO_ROOT)
        result = _run(env.env, _base_args(target, expected_sha=False))
        assert result.returncode != 0
        assert "--non-interactive requires --expected-sha" in result.stderr


# ---------------------------------------------------------------------------
# Dry run -- zero managed-state mutation.
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_makes_zero_mutation(self, tmp_path):
        env = seed_installed_environment(tmp_path)
        target = build_update_target_checkout(tmp_path / "target-dry", REPO_ROOT)
        result = _run(env.env, _base_args(target, extra=["--dry-run"]))
        assert result.returncode == 0, result.stderr
        assert "Application payload" in result.stdout
        assert not env.ct_file(FAKE_VMID, "/opt/hubinet-ops/.hubinet-source-commit").exists()
        assert env.state()["vmids"][FAKE_VMID]["service"] == "active"
        assert not any("systemctl" in line and "stop" in line for line in env.log_lines())


class TestOperatorPlanVirtualenvContract:
    def test_changed_requirements_describe_live_path_maintenance_build(self, tmp_path):
        env = seed_installed_environment(tmp_path, installed_requirements="old==1\n")
        target = build_update_target_checkout(
            tmp_path / "target-plan-reqs-changed", REPO_ROOT, requirements_text="new==2\n"
        )
        result = _run(env.env, _base_args(target, extra=["--dry-run"]))
        assert result.returncode == 0, result.stderr
        plan = result.stdout
        assert "service is stopped" in plan
        assert "current virtualenv is preserved for rollback" in plan
        assert "FINAL live path /opt/hubinet-ops/.venv" in plan
        assert "pip installs the exact target requirements DURING the maintenance window" in plan
        assert "can extend downtime" in plan
        assert "activation failure restores the prior environment" in plan
        assert "staged and swapped" not in plan
        assert "stage and swap" not in plan

    def test_unchanged_requirements_describe_no_dependency_install(self, tmp_path):
        env = seed_installed_environment(tmp_path)
        target = build_update_target_checkout(tmp_path / "target-plan-reqs-unchanged", REPO_ROOT)
        result = _run(env.env, _base_args(target, extra=["--dry-run"]))
        assert result.returncode == 0, result.stderr
        assert (
            "requirements.txt:              unchanged -- existing virtualenv is preserved; "
            "no virtualenv rebuild or pip/dependency installation occurs"
        ) in result.stdout


# ---------------------------------------------------------------------------
# Per-VMID kernel-backed single-flight. These are real simultaneously-held
# advisory locks, not a mocked "lock exists" flag.
# ---------------------------------------------------------------------------


class TestPerVmidSingleFlight:
    def test_same_vmid_second_invocation_is_rejected_until_holder_exits(self, tmp_path):
        env = seed_installed_environment(tmp_path)
        target = build_update_target_checkout(tmp_path / "target-lock", REPO_ROOT)
        first_stdout = tmp_path / "first.stdout"
        first_stderr = tmp_path / "first.stderr"
        first_args = [
            "bash",
            str(UPDATE_SCRIPT),
            "--vmid",
            FAKE_VMID,
            "--source-dir",
            str(target),
            "--expected-sha",
            git_head_sha(target),
        ]

        with first_stdout.open("wb") as stdout_fh, first_stderr.open("wb") as stderr_fh:
            first = subprocess.Popen(
                first_args,
                cwd=str(REPO_ROOT),
                env=env.env,
                stdin=subprocess.PIPE,
                stdout=stdout_fh,
                stderr=stderr_fh,
            )
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if "Hubinet Ops in-place update plan" in first_stdout.read_text(
                    encoding="utf-8", errors="replace"
                ):
                    break
                assert first.poll() is None, first_stderr.read_text(
                    encoding="utf-8", errors="replace"
                )
                time.sleep(0.05)
            else:
                first.kill()
                pytest.fail("first updater never reached its confirmation while holding the lock")

            log_count = len(env.log_lines())
            second = _run(env.env, _base_args(target, extra=["--dry-run"]))
            assert second.returncode != 0
            assert "another Hubinet Ops updater run owns VMID" in second.stderr
            assert len(env.log_lines()) == log_count, (
                "the rejected invocation must not reach ownership/planning fake PVE commands"
            )

            first.terminate()
            first.wait(timeout=15)

        later = _run(env.env, _base_args(target, extra=["--dry-run"]))
        assert later.returncode == 0, later.stderr

    def test_different_vmid_lock_does_not_reject_target_vmid(self, tmp_path):
        env = seed_installed_environment(tmp_path)
        target = build_update_target_checkout(tmp_path / "target-other-lock", REPO_ROOT)
        other_lock = _update_state_path(env, "111", "lock")
        other_lock.parent.mkdir(parents=True, exist_ok=True)
        with other_lock.open("w", encoding="utf-8") as lock_fh:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = _run(env.env, _base_args(target, extra=["--dry-run"]))
        assert result.returncode == 0, result.stderr

    def test_stale_unheld_lock_file_does_not_block_later_invocation(self, tmp_path):
        env = seed_installed_environment(tmp_path)
        target = build_update_target_checkout(tmp_path / "target-stale-lock", REPO_ROOT)
        stale_lock = _update_state_path(env, FAKE_VMID, "lock")
        stale_lock.parent.mkdir(parents=True, exist_ok=True)
        stale_lock.write_text("stale file without a kernel lock\n", encoding="utf-8")

        result = _run(env.env, _base_args(target, extra=["--dry-run"]))
        assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# Durable interrupted-run recovery. The fake performs the first destructive
# rename and then SIGKILLs the updater shell itself, bypassing EXIT entirely.
# ---------------------------------------------------------------------------


class TestInterruptedRunRecovery:
    def test_sigkill_after_live_app_move_is_recovered_before_new_plan(self, tmp_path):
        env = seed_installed_environment(
            tmp_path,
            installed_source_sha="6" * 40,
            scenario_overrides={"kill_updater_after_move": "mv_live_app_to_rollback"},
        )
        target = build_update_target_checkout(tmp_path / "target-sigkill", REPO_ROOT)

        interrupted = _run(env.env, _base_args(target))
        assert interrupted.returncode == -9
        journal = _update_state_path(env, FAKE_VMID, "journal")
        journal_text = journal.read_text(encoding="utf-8")
        assert "state=active" in journal_text
        assert "rollback_armed=1" in journal_text
        assert "ledger=update-app-activation-attempted" in journal_text
        assert not env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app").exists()
        assert list(env.ct_file(FAKE_VMID, "/opt/hubinet-ops").glob("app.rollback-*"))

        run_id = _journal_run_id(journal_text)

        before_recovery = len(env.log_lines())
        recovered = _run(env.env, _base_args(target))
        assert recovered.returncode == 0, recovered.stderr
        assert "detected prior updater journal" in recovered.stderr
        assert "previous interrupted update" in recovered.stderr
        assert "Phase U2" not in recovered.stderr
        assert env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app/__init__.py").exists()
        assert not env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app/inventory/store.py").exists()
        assert env.state()["vmids"][FAKE_VMID]["service"] == "active"
        assert not journal.exists()
        _assert_recovery_repushed_only_the_authority_tool(env, before_recovery, run_id)

    def test_sigkill_after_target_service_start_rolls_back_before_new_plan(self, tmp_path):
        env = seed_installed_environment(
            tmp_path,
            installed_source_sha="7" * 40,
            scenario_overrides={"kill_updater_after_target_start": True},
        )
        target = build_update_target_checkout(tmp_path / "target-late-sigkill", REPO_ROOT)

        interrupted = _run(env.env, _base_args(target))
        assert interrupted.returncode == -9
        assert env.state()["vmids"][FAKE_VMID]["service"] == "active"
        assert env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app/inventory/store.py").exists()
        journal = _update_state_path(env, FAKE_VMID, "journal")
        assert "state=active" in journal.read_text(encoding="utf-8")

        recovered = _run(env.env, _base_args(target))
        assert recovered.returncode == 0, recovered.stderr
        assert "previous interrupted update" in recovered.stderr
        assert "Phase U2" not in recovered.stderr
        assert env.state()["vmids"][FAKE_VMID]["service"] == "active"
        assert env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app/__init__.py").exists()
        assert not env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app/inventory/store.py").exists()
        assert not journal.exists()

    def test_unproven_recovery_retains_journal_and_blocks_new_plan(self, tmp_path):
        env = seed_installed_environment(
            tmp_path,
            scenario_overrides={"kill_updater_after_move": "mv_live_app_to_rollback"},
        )
        target = build_update_target_checkout(tmp_path / "target-unresolved", REPO_ROOT)
        interrupted = _run(env.env, _base_args(target))
        assert interrupted.returncode == -9

        scenario = json.loads(env.scenario_path.read_text(encoding="utf-8"))
        scenario["fail_service_state_probe_after"] = 1
        env.scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
        journal = _update_state_path(env, FAKE_VMID, "journal")
        run_id = _journal_run_id(journal.read_text(encoding="utf-8"))
        before_recovery = len(env.log_lines())

        blocked = _run(env.env, _base_args(target))
        assert blocked.returncode != 0
        assert "ROLLBACK COULD NOT BE COMPLETED" in blocked.stderr
        assert "active journal" in blocked.stderr
        assert "Phase U2" not in blocked.stderr
        assert journal.exists()
        assert "state=active" in journal.read_text(encoding="utf-8")
        assert not env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app").exists()
        assert list(env.ct_file(FAKE_VMID, "/opt/hubinet-ops").glob("app.rollback-*"))
        _assert_recovery_repushed_only_the_authority_tool(env, before_recovery, run_id)


# ---------------------------------------------------------------------------
# K/L. Installed-source marker semantics, and repeated in-place updates on
#      ONE synthetic installation -- the automated proof of
#      INSTALL ONCE -> UPDATE MANY TIMES.
# ---------------------------------------------------------------------------


class TestInstalledSourceMarkerAndRepeatedUpdates:
    def test_missing_marker_reports_unknown_and_gets_written_on_success(self, tmp_path):
        env = seed_installed_environment(tmp_path, installed_source_sha=None)
        target = build_update_target_checkout(tmp_path / "target-first", REPO_ROOT)
        result = _run(env.env, _base_args(target))
        assert result.returncode == 0, result.stderr
        assert "unknown (pre-updater install)" in result.stdout
        marker = env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/.hubinet-source-commit").strip()
        assert marker == git_head_sha(target)

    def test_failed_update_leaves_marker_unchanged(self, tmp_path):
        env = seed_installed_environment(tmp_path, installed_source_sha="2" * 40)
        target = build_update_target_checkout(
            tmp_path / "target-failed", REPO_ROOT,
            requirements_text="fastapi==0.999.0\n",
        )
        env2 = env
        # Force pip install (staging, before service stop) to fail.
        state = json.loads(env2.scenario_path.read_text(encoding="utf-8"))
        state["fail"] = ["pip_install"]
        env2.scenario_path.write_text(json.dumps(state), encoding="utf-8")
        result = _run(env2.env, _base_args(target))
        assert result.returncode != 0
        marker = env2.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/.hubinet-source-commit").strip()
        assert marker == "2" * 40

    def test_two_sequential_updates_on_one_installation(self, tmp_path):
        env = seed_installed_environment(tmp_path, installed_source_sha=None)
        pre_state = env.state()

        target_a = build_update_target_checkout(tmp_path / "target-a", REPO_ROOT)
        result_a = _run(env.env, _base_args(target_a))
        assert result_a.returncode == 0, result_a.stderr
        sha_a = env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/.hubinet-source-commit").strip()
        assert sha_a == git_head_sha(target_a)
        # The updater temporarily disables boot activation for its mutation
        # window; every update must hand the installation back ENABLED, or
        # the next PVE/CT restart would silently never bring Hubinet back.
        assert env.state()["vmids"][FAKE_VMID]["service_enabled"] is True

        target_b = build_update_target_checkout(
            tmp_path / "target-b", REPO_ROOT, requirements_text="fastapi==0.200.0\n"
        )
        result_b = _run(env.env, _base_args(target_b))
        assert result_b.returncode == 0, result_b.stderr
        sha_b = env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/.hubinet-source-commit").strip()
        assert sha_b == git_head_sha(target_b)
        assert sha_b != sha_a
        assert env.state()["vmids"][FAKE_VMID]["service_enabled"] is True

        post_state = env.state()
        assert post_state["pve_users"] == pre_state["pve_users"]
        assert post_state["pve_tokens"] == pre_state["pve_tokens"]
        assert post_state["vmids"][FAKE_VMID]["service"] == "active"
        db = json.loads(env.ct_file_text(FAKE_VMID, "/var/lib/hubinet-ops/authority.db"))
        assert db["backend_instance_id"] == FAKE_BACKEND_INSTANCE_ID


# ---------------------------------------------------------------------------
# M. P1-A -- every INTERMEDIATE activation-step failure (not only ones
#    after a fully completed artifact swap) must roll back to a coherent
#    OLD installation. See deploy/lib/update-activate.sh's own header
#    comment and _update_rollback_app / _update_rollback_venv_and_
#    requirements / _update_rollback_unit.
# ---------------------------------------------------------------------------


class TestActivationIntermediateFailures:
    def test_app_first_move_failure_leaves_old_app_untouched(self, tmp_path):
        env = seed_installed_environment(
            tmp_path,
            installed_source_sha="3" * 40,
            scenario_overrides={"fail": ["mv_live_app_to_rollback"]},
        )
        target = build_update_target_checkout(tmp_path / "target-app-fail-1", REPO_ROOT)
        result = _run(env.env, _base_args(target))
        assert result.returncode != 0
        assert env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app/__init__.py").exists()
        assert not env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app/inventory/store.py").exists()
        assert env.state()["vmids"][FAKE_VMID]["service"] == "active"
        marker = env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/.hubinet-source-commit").strip()
        assert marker == "3" * 40

    def test_app_second_move_failure_restores_old_app(self, tmp_path):
        # The concrete witness P1-A targets: by the time the SECOND move
        # (staged -> live) fails, the OLD app has already been moved aside
        # to app.rollback-<RUN_ID>. Under the old code, the
        # "update-app-activated" ledger marker was only ever recorded
        # AFTER this second move succeeded, so rollback would skip app
        # restoration entirely and leave /opt/hubinet-ops/app missing
        # outright.
        env = seed_installed_environment(
            tmp_path,
            installed_source_sha="4" * 40,
            scenario_overrides={"fail": ["mv_staged_app_to_live"]},
        )
        target = build_update_target_checkout(tmp_path / "target-app-fail-2", REPO_ROOT)
        result = _run(env.env, _base_args(target))
        assert result.returncode != 0
        assert env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app").exists(), (
            "the application payload directory must never be left missing after rollback"
        )
        assert env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app/__init__.py").exists()
        assert not env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app/inventory/store.py").exists()
        assert env.state()["vmids"][FAKE_VMID]["service"] == "active"

    def test_venv_build_step_failure_restores_old_venv(self, tmp_path):
        # Correction pass 8 (P1): the venv step's second half is no longer
        # a staged->live rename but the BUILD at the final live path, so
        # this family's "the first destructive move succeeded and the
        # activating step then failed" state is reached that way now. The
        # invariant under test is unchanged: rollback must never leave the
        # virtualenv missing, and requirements.txt must still describe the
        # environment that is actually installed.
        env = seed_installed_environment(
            tmp_path,
            installed_requirements="fastapi==0.100.0\n",
            scenario_overrides={"fail": ["pip_install"]},
        )
        target = build_update_target_checkout(
            tmp_path / "target-venv-fail", REPO_ROOT, requirements_text="fastapi==0.116.1\n"
        )
        result = _run(env.env, _base_args(target))
        assert result.returncode != 0
        assert env.ct_file(FAKE_VMID, "/opt/hubinet-ops/.venv/bin/pip").exists(), (
            "the virtualenv must never be left missing after rollback"
        )
        assert env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/requirements.txt") == "fastapi==0.100.0\n"
        assert env.state()["vmids"][FAKE_VMID]["service"] == "active"

    def test_requirements_second_move_failure_restores_old_requirements(self, tmp_path):
        env = seed_installed_environment(
            tmp_path,
            installed_requirements="fastapi==0.100.0\n",
            scenario_overrides={"fail": ["mv_staged_requirements_to_live"]},
        )
        target = build_update_target_checkout(
            tmp_path / "target-reqs-fail-2", REPO_ROOT, requirements_text="fastapi==0.116.1\n"
        )
        result = _run(env.env, _base_args(target))
        assert result.returncode != 0
        assert env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/requirements.txt") == "fastapi==0.100.0\n"
        assert env.ct_file(FAKE_VMID, "/opt/hubinet-ops/.venv/bin/pip").exists()
        assert env.state()["vmids"][FAKE_VMID]["service"] == "active"

    def test_unit_preserve_copy_failure_leaves_old_unit(self, tmp_path):
        env = seed_installed_environment(
            tmp_path, scenario_overrides={"fail": ["cp_live_unit_to_rollback"]}
        )
        target = build_update_target_checkout(
            tmp_path / "target-unit-fail", REPO_ROOT, unit_text="[Unit]\nDescription=changed\n"
        )
        result = _run(env.env, _base_args(target))
        assert result.returncode != 0
        assert "changed" not in env.ct_file_text(FAKE_VMID, "/etc/systemd/system/hubinet-ops.service")
        assert env.state()["vmids"][FAKE_VMID]["service"] == "active"


# ---------------------------------------------------------------------------
# N. P1-B -- authority-database removal during ROLLBACK must fail closed:
#    the validated pre-update backup must never be copied over an
#    unproven live/sidecar state.
# ---------------------------------------------------------------------------


class TestAuthorityRemoveFailClosedRollback:
    def test_rollback_hard_stops_when_target_db_removal_cannot_be_proven(self, tmp_path):
        # The forward reset's own `remove` call (1st) succeeds; the LATER
        # rollback-triggering failure's `remove` call (2nd, during
        # rollback itself) is the one that fails here.
        env = seed_installed_environment(
            tmp_path,
            schema_version=9,
            scenario_overrides={
                "discovery_result": "backend_unreachable",
                "fail_nth_authority_remove": 2,
            },
        )
        target = build_update_target_checkout(tmp_path / "target-v10-remove-fail", REPO_ROOT, schema_version=10)
        result = _run(env.env, _base_args(target, extra=["--allow-authority-reset"]))
        assert result.returncode != 0
        assert "ROLLBACK COULD NOT BE COMPLETED" in result.stderr
        assert "could not prove removal" in result.stderr
        backups_root = env.ct_file(FAKE_VMID, "/var/lib/hubinet-ops/update-backups")
        backup_files = list(backups_root.rglob("authority.db"))
        assert backup_files, "expected the retained authority DB backup to survive"
        backup_data = json.loads(backup_files[0].read_text(encoding="utf-8"))
        assert backup_data["schema_version"] == 9
        assert backup_data["backend_instance_id"] == FAKE_BACKEND_INSTANCE_ID
        # Never copied over an uncertain live database state.
        assert not env.ct_file(FAKE_VMID, "/var/lib/hubinet-ops/authority.db").exists()


# ---------------------------------------------------------------------------
# N2. P1-A (correction pass 2) -- the authority-reset "attempted" marker
#     must be recorded AFTER the validated backup but BEFORE the first
#     destructive removal, so an INTERMEDIATE failure inside `remove`
#     itself (one sidecar path already unlinked, a later one failing)
#     still triggers a coherent rollback of the authority database instead
#     of silently skipping it because no marker was ever recorded.
# ---------------------------------------------------------------------------


class TestAuthorityResetAttemptedBeforeDestructiveRemoveP1A:
    def test_rollback_restores_old_db_after_intermediate_remove_failure(self, tmp_path):
        # Witness: the coherent backup succeeds; the forward reset's own
        # `remove` call (1st) is the one that fails PARTWAY THROUGH --
        # the fake's "fail_nth_authority_remove_partial" seam actually
        # unlinks the underlying db path before reporting "ok": false,
        # exactly like a real cmd_remove() whose db unlink succeeded but
        # whose -wal/-shm unlink then failed. Under the old code, the
        # "update-authority-reset" ledger marker was only ever recorded
        # AFTER a fully successful `remove`, so this die would leave NO
        # marker recorded and rollback would skip authority restoration
        # outright, leaving old code paired with a missing database.
        env = seed_installed_environment(
            tmp_path,
            schema_version=9,
            scenario_overrides={"fail_nth_authority_remove_partial": 1},
        )
        target = build_update_target_checkout(tmp_path / "target-v10-partial-remove", REPO_ROOT, schema_version=10)
        result = _run(env.env, _base_args(target, extra=["--allow-authority-reset"]))
        assert result.returncode != 0

        db = json.loads(env.ct_file_text(FAKE_VMID, "/var/lib/hubinet-ops/authority.db"))
        assert db["schema_version"] == 9, (
            "the OLD authority database must be restored even when the forward reset's own "
            "`remove` call failed partway through (one path already unlinked)"
        )
        assert db["backend_instance_id"] == FAKE_BACKEND_INSTANCE_ID
        assert env.state()["vmids"][FAKE_VMID]["service"] == "active"
        assert not env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app/inventory/store.py").exists()


# ---------------------------------------------------------------------------
# M2. P1-B (correction pass 2) -- the systemd unit's preserving `cp` is not
#     atomic; rollback must never treat a PARTIAL destination left behind
#     by a failed copy as complete, trustworthy pre-update state.
# ---------------------------------------------------------------------------


class TestSystemdUnitPartialPreservingCopyP1B:
    def test_partial_preserving_copy_never_corrupts_rollback(self, tmp_path):
        env = seed_installed_environment(
            tmp_path, scenario_overrides={"fail": ["cp_live_unit_to_rollback_partial"]}
        )
        pre_unit = env.ct_file_text(FAKE_VMID, "/etc/systemd/system/hubinet-ops.service")
        target = build_update_target_checkout(
            tmp_path / "target-unit-partial-fail", REPO_ROOT, unit_text="[Unit]\nDescription=changed\n"
        )
        result = _run(env.env, _base_args(target))
        assert result.returncode != 0
        # The live unit must remain byte-identical -- rollback must not
        # have replaced it with the partial backup (there was nothing
        # valid to roll back to in the first place: the canonical
        # rollback-<UPDATE_RUN_ID> path is only ever finalized AFTER a
        # fully successful preserving copy).
        assert env.ct_file_text(FAKE_VMID, "/etc/systemd/system/hubinet-ops.service") == pre_unit
        assert "changed" not in env.ct_file_text(FAKE_VMID, "/etc/systemd/system/hubinet-ops.service")
        assert env.state()["vmids"][FAKE_VMID]["service"] == "active"


# ---------------------------------------------------------------------------
# O. P2-A -- staging must be exact and run-owned/clean.
# ---------------------------------------------------------------------------


class TestStagingExactAndCleanP2A:
    def test_stale_legacy_shared_staging_path_never_enters_activated_app(self, tmp_path):
        env = seed_installed_environment(tmp_path)
        stale = env.ct_file(FAKE_VMID, "/tmp/hubinet-ops-update-src/leftover.py")
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text("# stale content from a prior interrupted run\n", encoding="utf-8")
        target = build_update_target_checkout(tmp_path / "target-stale-staging", REPO_ROOT)
        result = _run(env.env, _base_args(target))
        assert result.returncode == 0, result.stderr
        assert not env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app/leftover.py").exists()
        # The stale fixed-name path is simply never touched by a run-owned
        # (UPDATE_RUN_ID-suffixed) staging path.
        assert stale.exists()

    def test_helper_staged_from_exact_commit_not_dirty_worktree(self, tmp_path):
        env = seed_installed_environment(tmp_path)
        target = build_update_target_checkout(
            tmp_path / "target-helper-exact", REPO_ROOT,
            helper_text="#!/usr/bin/env python3\n# committed helper content\n",
        )
        expected_sha = git_head_sha(target)
        # `git status --porcelain` (this repo's own clean-worktree gate)
        # reports clean for an assume-unchanged path even though its
        # on-disk content differs from what is actually committed at
        # HEAD -- staging must read the committed blob (git show), never
        # this drifted worktree file.
        subprocess.run(
            ["git", "update-index", "--assume-unchanged", "deploy/hubinet-package-scan-helper.py"],
            cwd=str(target), check=True, capture_output=True,
        )
        (target / "deploy" / "hubinet-package-scan-helper.py").write_text(
            "#!/usr/bin/env python3\n# WORKTREE DRIFT -- must never be staged\n", encoding="utf-8"
        )
        result = _run(env.env, _base_args(target, expected_sha=expected_sha))
        assert result.returncode == 0, result.stderr
        helper_path = (
            Path(env.env["HUBINET_OPS_TEST_HOST_ROOT"])
            / "usr" / "local" / "libexec" / f"hubinet-package-scan-helper-{FAKE_RUN_ID}"
        )
        activated = helper_path.read_text(encoding="utf-8")
        assert "WORKTREE DRIFT" not in activated
        assert "committed helper content" in activated


# ---------------------------------------------------------------------------
# P. P2-B -- the installed-source marker must roll back coherently,
#    together with the app/db, never leaving a NEW marker paired with a
#    rolled-back OLD installation.
# ---------------------------------------------------------------------------


class TestMarkerRollbackCoherenceP2B:
    def test_marker_move_failure_restores_old_marker_with_app(self, tmp_path):
        env = seed_installed_environment(
            tmp_path,
            installed_source_sha="5" * 40,
            scenario_overrides={"fail": ["mv_staged_marker_to_live"]},
        )
        target = build_update_target_checkout(tmp_path / "target-marker-fail", REPO_ROOT)
        result = _run(env.env, _base_args(target))
        assert result.returncode != 0
        marker = env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/.hubinet-source-commit").strip()
        assert marker == "5" * 40
        # The app payload -- already fully activated to the NEW content by
        # the time the marker step (the LAST activation step) runs -- must
        # also have been rolled back to the OLD content, together with
        # the marker.
        assert not env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app/inventory/store.py").exists()
        assert env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app/__init__.py").exists()
        assert env.state()["vmids"][FAKE_VMID]["service"] == "active"

    def test_marker_move_failure_with_no_pre_existing_marker_leaves_none(self, tmp_path):
        env = seed_installed_environment(
            tmp_path,
            installed_source_sha=None,
            scenario_overrides={"fail": ["mv_staged_marker_to_live"]},
        )
        target = build_update_target_checkout(tmp_path / "target-marker-fail-no-old", REPO_ROOT)
        result = _run(env.env, _base_args(target))
        assert result.returncode != 0
        assert not env.ct_file(FAKE_VMID, "/opt/hubinet-ops/.hubinet-source-commit").exists()
        assert env.state()["vmids"][FAKE_VMID]["service"] == "active"


# ---------------------------------------------------------------------------
# Q. P2-C -- a schema-PRESERVING update must preflight-prove the live DB's
#    actual structural schema objects match the target's required set,
#    before the service is ever stopped -- a coherent marker/version/
#    backend-identity classification alone is weaker than the target
#    runtime's own schema validation.
# ---------------------------------------------------------------------------


class TestPreserveSchemaObjectsP2C:
    def test_preserve_fails_closed_before_service_stop_on_structural_drift(self, tmp_path):
        env = seed_installed_environment(
            tmp_path,
            schema_version=10,
            # Missing "one_active_endpoint_per_source" relative to the
            # target's default required set (FAKE_REQUIRED_SCHEMA_OBJECTS).
            schema_objects=["authority_schema", "backend_instance"],
        )
        target = build_update_target_checkout(tmp_path / "target-schema-drift", REPO_ROOT, schema_version=10)
        result = _run(env.env, _base_args(target))
        assert result.returncode != 0
        assert "structurally drifted" in result.stderr
        assert env.state()["vmids"][FAKE_VMID]["service"] == "active"
        assert not any("systemctl" in line and "stop" in line for line in env.log_lines())

    def test_preserve_succeeds_when_schema_objects_match(self, tmp_path):
        env = seed_installed_environment(tmp_path)
        target = build_update_target_checkout(tmp_path / "target-schema-ok", REPO_ROOT)
        result = _run(env.env, _base_args(target))
        assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# R. P2 (correction pass 2) -- artifact classification must be genuinely
#    byte-exact. deploy/lib/update-plan.sh previously classified
#    requirements.txt/the systemd unit/the PVE helper by comparing content
#    captured through bash command substitution, which silently strips
#    trailing newline bytes -- two files differing ONLY in a trailing
#    newline could be misclassified "unchanged" and so never replaced. An
#    artifact classified "unchanged" is never touched at all, so if this
#    regressed, the installed content below would still be the ORIGINAL
#    (pre-update) text rather than the target's -- see
#    deploy/lib/update-plan.sh's _update_files_differ_exact /
#    _update_target_file_to_file / _update_installed_ct_file_to_file.
# ---------------------------------------------------------------------------


class TestByteExactClassificationP2:
    def test_requirements_trailing_newline_only_change_is_detected(self, tmp_path):
        env = seed_installed_environment(tmp_path, installed_requirements="fastapi==0.116.1\n")
        target_text = "fastapi==0.116.1\n\n"
        target = build_update_target_checkout(
            tmp_path / "target-reqs-newline", REPO_ROOT, requirements_text=target_text,
        )
        result = _run(env.env, _base_args(target))
        assert result.returncode == 0, result.stderr
        assert env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/requirements.txt") == target_text

    def test_unit_trailing_newline_only_change_is_detected(self, tmp_path):
        installed_text = "[Unit]\nDescription=hubinet-ops\n"
        target_text = installed_text + "\n"
        env = seed_installed_environment(tmp_path, installed_unit_text=installed_text)
        target = build_update_target_checkout(
            tmp_path / "target-unit-newline", REPO_ROOT, unit_text=target_text,
        )
        result = _run(env.env, _base_args(target))
        assert result.returncode == 0, result.stderr
        assert env.ct_file_text(FAKE_VMID, "/etc/systemd/system/hubinet-ops.service") == target_text
        assert any("daemon-reload" in line for line in env.log_lines())

    def test_helper_trailing_newline_only_change_is_detected(self, tmp_path):
        installed_text = "#!/usr/bin/env python3\n# helper\n"
        target_text = installed_text + "\n"
        env = seed_installed_environment(tmp_path, installed_helper_text=installed_text)
        helper_path = (
            Path(env.env["HUBINET_OPS_TEST_HOST_ROOT"])
            / "usr" / "local" / "libexec" / f"hubinet-package-scan-helper-{FAKE_RUN_ID}"
        )
        target = build_update_target_checkout(
            tmp_path / "target-helper-newline", REPO_ROOT, helper_text=target_text,
        )
        result = _run(env.env, _base_args(target))
        assert result.returncode == 0, result.stderr
        assert helper_path.read_text(encoding="utf-8") == target_text


# ---------------------------------------------------------------------------
# S. Correction pass 3 -- service runtime state is three-valued and rollback
#    is armed before the first stop request, not after a success-only result.
# ---------------------------------------------------------------------------


class TestServiceStateMachineCorrectionPass3:
    def test_stop_mutates_then_fails_still_runs_recovery(self, tmp_path):
        env = seed_installed_environment(
            tmp_path,
            scenario_overrides={"fail": ["service_stop_mutate_then_fail"]},
        )
        target = build_update_target_checkout(tmp_path / "target-stop-mutate-fail", REPO_ROOT)
        result = _run(env.env, _base_args(target))

        assert result.returncode != 0
        assert "rollback complete" in result.stderr
        assert "before any service stop was attempted" not in result.stderr
        assert env.state()["service_stop_calls"] == 2
        assert env.state()["vmids"][FAKE_VMID]["service"] == "active"
        assert env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app/__init__.py").exists()
        assert not env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app/inventory/store.py").exists()

    def test_target_start_mutates_then_fails_is_stopped_and_rolled_back(self, tmp_path):
        env = seed_installed_environment(
            tmp_path,
            scenario_overrides={"fail": ["service_start_mutate_then_fail"]},
        )
        target = build_update_target_checkout(tmp_path / "target-start-mutate-fail", REPO_ROOT)
        result = _run(env.env, _base_args(target))

        assert result.returncode != 0
        assert "rollback complete" in result.stderr
        state = env.state()
        assert state["service_start_calls"] == 2
        assert state["service_stop_calls"] == 2
        assert state["vmids"][FAKE_VMID]["service"] == "active"
        assert not env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app/inventory/store.py").exists()

        lines = env.log_lines()
        rollback_stop = next(
            i for i, line in enumerate(lines)
            if "systemctl stop hubinet-ops" in line
            and i > next(j for j, row in enumerate(lines) if "systemctl start hubinet-ops" in row)
        )
        stopped_proof = next(
            i for i, line in enumerate(lines[rollback_stop + 1 :], rollback_stop + 1)
            if "systemctl show hubinet-ops --property=ActiveState --value" in line
        )
        first_app_mutation = next(
            i for i, line in enumerate(lines[rollback_stop + 1 :], rollback_stop + 1)
            if "rm -rf /opt/hubinet-ops/app" in line
        )
        assert rollback_stop < stopped_proof < first_app_mutation

    def test_rollback_stop_failure_hard_stops_before_any_rollback_mutation(self, tmp_path):
        env = seed_installed_environment(
            tmp_path,
            scenario_overrides={
                "discovery_result": "backend_unreachable",
                "fail_nth_service_stop": 2,
            },
        )
        target = build_update_target_checkout(tmp_path / "target-rollback-stop-fail", REPO_ROOT)
        result = _run(env.env, _base_args(target))

        assert result.returncode != 0
        assert "ROLLBACK COULD NOT BE COMPLETED" in result.stderr
        assert "positively prove hubinet-ops non-running" in result.stderr
        assert "rollback complete" not in result.stderr
        assert env.state()["vmids"][FAKE_VMID]["service"] == "active"
        assert env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app/inventory/store.py").exists()
        rollback_artifacts = list(
            env.ct_file(FAKE_VMID, "/opt/hubinet-ops").glob("app.rollback-*")
        )
        assert rollback_artifacts
        assert not any("rm -rf /opt/hubinet-ops/app" in line for line in env.log_lines())

    def test_unknown_service_state_is_never_treated_as_stopped(self, tmp_path):
        env = seed_installed_environment(
            tmp_path,
            scenario_overrides={
                "discovery_result": "backend_unreachable",
                # Forward stop proof is call 1. Every rollback probe is
                # then an unknown outer-command failure.
                "fail_service_state_probe_after": 1,
            },
        )
        target = build_update_target_checkout(tmp_path / "target-state-unknown", REPO_ROOT)
        result = _run(env.env, _base_args(target))

        assert result.returncode != 0
        assert "ROLLBACK COULD NOT BE COMPLETED" in result.stderr
        assert "unknown (probe exit" in result.stderr
        assert "rollback complete" not in result.stderr
        assert env.state()["vmids"][FAKE_VMID]["service"] == "inactive"
        assert env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app/inventory/store.py").exists()
        assert list(env.ct_file(FAKE_VMID, "/opt/hubinet-ops").glob("app.rollback-*"))
        assert not any("rm -rf /opt/hubinet-ops/app" in line for line in env.log_lines())


# ---------------------------------------------------------------------------
# T. Correction pass 3 -- rollback path existence and removal postconditions
#    are three-valued and fail closed before a restore rename.
# ---------------------------------------------------------------------------


class TestRollbackPathProofCorrectionPass3:
    def test_path_probe_transport_failure_preserves_target_and_rollback_artifact(self, tmp_path):
        env = seed_installed_environment(
            tmp_path,
            scenario_overrides={
                "discovery_result": "backend_unreachable",
                "path_probe_transport_fail_prefixes": [
                    "/opt/hubinet-ops/app.rollback-"
                ],
            },
        )
        target = build_update_target_checkout(tmp_path / "target-path-probe-unknown", REPO_ROOT)
        result = _run(env.env, _base_args(target))

        assert result.returncode != 0
        assert "ROLLBACK COULD NOT BE COMPLETED" in result.stderr
        assert "could not determine whether the pre-update application rollback artifact exists" in result.stderr
        assert "rollback complete" not in result.stderr
        assert env.state()["vmids"][FAKE_VMID]["service"] == "inactive"
        assert env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app/inventory/store.py").exists()
        rollback_artifacts = list(env.ct_file(FAKE_VMID, "/opt/hubinet-ops").glob("app.rollback-*"))
        assert rollback_artifacts
        assert not any("rm -rf /opt/hubinet-ops/app" in line for line in env.log_lines())

    def test_partial_rm_hard_stops_without_nesting_rollback_directory(self, tmp_path):
        env = seed_installed_environment(
            tmp_path,
            scenario_overrides={
                "discovery_result": "backend_unreachable",
                "fail": ["rm_live_app_partial"],
            },
        )
        target = build_update_target_checkout(tmp_path / "target-partial-rm", REPO_ROOT)
        result = _run(env.env, _base_args(target))

        assert result.returncode != 0
        assert "ROLLBACK COULD NOT BE COMPLETED" in result.stderr
        assert "path still exists, so restoration was not attempted" in result.stderr
        assert "rollback complete" not in result.stderr
        assert env.state()["vmids"][FAKE_VMID]["service"] == "inactive"
        live_app = env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app")
        rollback_artifacts = list(live_app.parent.glob("app.rollback-*"))
        assert live_app.is_dir()
        assert rollback_artifacts
        assert not list(live_app.glob("app.rollback-*")), "rollback directory must never be nested"
        lines = env.log_lines()
        rm_index = next(i for i, line in enumerate(lines) if "rm -rf /opt/hubinet-ops/app" in line)
        assert not any(
            "mv /opt/hubinet-ops/app.rollback-" in line
            for line in lines[rm_index + 1 :]
        )


# ---------------------------------------------------------------------------
# U. Correction pass 3 -- restored unit reload and authority DB metadata are
#    load-bearing rollback steps.
# ---------------------------------------------------------------------------


class TestRollbackLoadBearingStepsCorrectionPass3:
    def test_rollback_daemon_reload_failure_prevents_service_restart(self, tmp_path):
        env = seed_installed_environment(
            tmp_path,
            scenario_overrides={
                "discovery_result": "backend_unreachable",
                "fail_nth_daemon_reload": 2,
            },
        )
        target = build_update_target_checkout(
            tmp_path / "target-rollback-reload-fail",
            REPO_ROOT,
            unit_text="[Unit]\nDescription=target unit\n",
        )
        result = _run(env.env, _base_args(target))

        assert result.returncode != 0
        assert "ROLLBACK COULD NOT BE COMPLETED" in result.stderr
        assert "daemon-reload failed after restoring the pre-update unit" in result.stderr
        assert "rollback complete" not in result.stderr
        state = env.state()
        assert state["daemon_reload_calls"] == 2
        assert state["service_start_calls"] == 1
        assert state["vmids"][FAKE_VMID]["service"] == "inactive"
        assert "target unit" not in env.ct_file_text(
            FAKE_VMID, "/etc/systemd/system/hubinet-ops.service"
        )
        assert list(env.ct_file(FAKE_VMID, "/opt/hubinet-ops").glob("app.rollback-*"))

    @pytest.mark.parametrize(
        ("failure_key", "diagnostic"),
        [
            ("authority_restore_chown", "chown hubinetops:hubinetops"),
            ("authority_restore_chmod", "chmod 0640"),
        ],
    )
    def test_authority_restore_metadata_failure_is_attributed_and_hard_stops(
        self, tmp_path, failure_key, diagnostic
    ):
        env = seed_installed_environment(
            tmp_path,
            schema_version=9,
            scenario_overrides={
                "discovery_result": "backend_unreachable",
                "fail": [failure_key],
            },
        )
        target = build_update_target_checkout(
            tmp_path / failure_key, REPO_ROOT, schema_version=10
        )
        result = _run(
            env.env,
            _base_args(target, extra=["--allow-authority-reset"]),
        )

        assert result.returncode != 0
        assert "ROLLBACK COULD NOT BE COMPLETED" in result.stderr
        assert diagnostic in result.stderr
        assert "rollback complete" not in result.stderr
        assert env.state()["vmids"][FAKE_VMID]["service"] == "inactive"
        restored = json.loads(
            env.ct_file_text(FAKE_VMID, "/var/lib/hubinet-ops/authority.db")
        )
        assert restored["schema_version"] == 9
        assert list(env.ct_file(FAKE_VMID, "/opt/hubinet-ops").glob("app.rollback-*"))


# ---------------------------------------------------------------------------
# V. Correction pass 3 -- cmp is preflighted and its 0/1/>1 outcomes are
#    equal/different/error, respectively.
# ---------------------------------------------------------------------------


class TestExactComparatorCorrectionPass3:
    def test_cmp_zero_classifies_equal(self, tmp_path):
        env = seed_installed_environment(tmp_path)
        target = build_update_target_checkout(tmp_path / "target-cmp-equal", REPO_ROOT)
        result = _run(env.env, _base_args(target, extra=["--dry-run"]))
        assert result.returncode == 0, result.stderr
        assert "requirements.txt:              unchanged" in result.stdout
        assert "systemd unit:                  unchanged" in result.stdout
        assert "PVE host helper:               unchanged" in result.stdout

    def test_cmp_one_classifies_different(self, tmp_path):
        env = seed_installed_environment(tmp_path, installed_requirements="old==1\n")
        target = build_update_target_checkout(
            tmp_path / "target-cmp-different", REPO_ROOT, requirements_text="new==2\n"
        )
        result = _run(env.env, _base_args(target, extra=["--dry-run"]))
        assert result.returncode == 0, result.stderr
        assert "requirements.txt:              changed" in result.stdout

    def test_cmp_exit_two_fails_planning_before_service_mutation(self, tmp_path):
        env = seed_installed_environment(
            tmp_path, scenario_overrides={"fail": ["cmp_error"]}
        )
        target = build_update_target_checkout(tmp_path / "target-cmp-error", REPO_ROOT)
        result = _run(env.env, _base_args(target))

        assert result.returncode != 0
        assert "exact comparison failed" in result.stderr
        assert "cmp exit 2" in result.stderr
        assert env.state()["vmids"][FAKE_VMID]["service"] == "active"
        assert not any("systemctl stop hubinet-ops" in line for line in env.log_lines())

    def test_missing_cmp_fails_preflight(self, tmp_path):
        env = seed_installed_environment(tmp_path)
        target = build_update_target_checkout(tmp_path / "target-cmp-missing", REPO_ROOT)
        preflight_bin = tmp_path / "preflight-bin"
        preflight_bin.mkdir()
        required_before_cmp = {
            "bash": shutil.which("bash"),
            "dirname": shutil.which("dirname"),
            "git": shutil.which("git"),
            "python3": shutil.which("python3"),
            "pct": str(env.bin_dir / "pct"),
            "pveum": str(env.bin_dir / "pveum"),
        }
        for name, source in required_before_cmp.items():
            assert source is not None
            (preflight_bin / name).symlink_to(source)

        missing_env = dict(env.env)
        missing_env["PATH"] = str(preflight_bin)
        result = subprocess.run(
            [
                required_before_cmp["bash"],
                str(UPDATE_SCRIPT),
                *_base_args(target),
            ],
            cwd=str(REPO_ROOT),
            env=missing_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode != 0
        assert "required command 'cmp' not found" in result.stderr
        assert not env.log_lines()


# ---------------------------------------------------------------------------
# N. Temporary service-autostart guard -- the PVE/CT reboot family.
#
#    The durable journal already reconnects a LATER updater invocation to an
#    interrupted run. It cannot, by itself, stop systemd from boot-activating
#    a half-swapped installation first: bootstrap leaves the CT at onboot=1
#    and hubinet-ops.service enabled, so a PVE power loss mid-update used to
#    mean "CT comes back, enabled unit auto-starts, target app runs against
#    the old venv / partial unit / partial database" long before any operator
#    reran the updater.
#
#    deploy/lib/update-activate.sh's _update_disable_service_autostart closes
#    that window with the minimum existing systemd mechanism: the unit is
#    `disable`d as the FIRST mutation and stays disabled through target
#    start, acceptance and source-marker activation; boot activation is
#    restored and POSITIVELY proven only on a fully accepted target, or by
#    rollback. The CT's own onboot flag is never touched.
#
#    Every test below drives one real interruption and then one real
#    simulated PVE/CT restart through the existing fake environment's own
#    bounded FakePveEnvironment.simulate_pve_ct_reboot action.
# ---------------------------------------------------------------------------


class TestServiceAutostartGuardAgainstReboot:
    def test_reboot_after_live_app_moved_aside_cannot_autostart_mixed_runtime(self, tmp_path):
        env = seed_installed_environment(
            tmp_path,
            installed_source_sha="a" * 40,
            scenario_overrides={"kill_updater_after_move": "mv_live_app_to_rollback"},
        )
        target = build_update_target_checkout(tmp_path / "target-reboot-a", REPO_ROOT)

        interrupted = _run(env.env, _base_args(target))
        assert interrupted.returncode == -9
        journal = _update_state_path(env, FAKE_VMID, "journal")
        journal_text = journal.read_text(encoding="utf-8")
        # Armed by the autostart guard itself, before the disable request.
        assert "ledger=update-service-autostart-disable-attempted" in journal_text
        assert "rollback_armed=1" in journal_text
        # Mid-mutation state: no live app at all, boot activation removed.
        assert not env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app").exists()
        assert env.state()["vmids"][FAKE_VMID]["service_enabled"] is False
        run_id = _journal_run_id(journal_text)

        env.simulate_pve_ct_reboot(FAKE_VMID)
        rebooted = env.state()["vmids"][FAKE_VMID]
        assert rebooted["started"] is True, "the CT still auto-starts: onboot is never changed"
        assert rebooted["service"] == "inactive", (
            "a disabled hubinet-ops must NOT be boot-activated against a "
            "half-swapped installation"
        )
        assert not env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app").exists()

        before_recovery = len(env.log_lines())
        recovered = _run(env.env, _base_args(target))
        assert recovered.returncode == 0, recovered.stderr
        assert "detected prior updater journal" in recovered.stderr
        assert "previous interrupted update" in recovered.stderr
        assert "Phase U2" not in recovered.stderr, "recovery must not start a new plan"
        assert env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app/__init__.py").exists()
        assert not env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app/inventory/store.py").exists()
        post = env.state()["vmids"][FAKE_VMID]
        assert post["service"] == "active"
        assert post["service_enabled"] is True
        assert not journal.exists()
        _assert_recovery_repushed_only_the_authority_tool(env, before_recovery, run_id)

    def test_reboot_after_app_activation_before_venv_swap_never_autostarts(self, tmp_path):
        env = seed_installed_environment(
            tmp_path,
            installed_source_sha="b" * 40,
            installed_requirements="fastapi==0.100.0\n",
            scenario_overrides={"kill_updater_after_move": "mv_staged_app_to_live"},
        )
        target = build_update_target_checkout(
            tmp_path / "target-reboot-b", REPO_ROOT, requirements_text="fastapi==0.116.1\n"
        )

        interrupted = _run(env.env, _base_args(target))
        assert interrupted.returncode == -9
        # The exact incoherent pairing this guard exists for: the TARGET app
        # is live, but the venv/requirements swap has not happened yet.
        assert env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app/inventory/store.py").exists()
        assert (
            env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/requirements.txt")
            == "fastapi==0.100.0\n"
        )
        journal = _update_state_path(env, FAKE_VMID, "journal")
        journal_text = journal.read_text(encoding="utf-8")
        assert "ledger=update-service-autostart-disable-attempted" in journal_text
        assert "ledger=update-venv-activation-attempted" not in journal_text

        env.simulate_pve_ct_reboot(FAKE_VMID)
        rebooted = env.state()["vmids"][FAKE_VMID]
        assert rebooted["started"] is True
        assert rebooted["service"] == "inactive", (
            "new app + old venv must never be auto-started after a reboot"
        )

        recovered = _run(env.env, _base_args(target))
        assert recovered.returncode == 0, recovered.stderr
        assert "Phase U2" not in recovered.stderr
        assert env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app/__init__.py").exists()
        assert not env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app/inventory/store.py").exists()
        assert (
            env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/requirements.txt")
            == "fastapi==0.100.0\n"
        )
        post = env.state()["vmids"][FAKE_VMID]
        assert post["service"] == "active"
        assert post["service_enabled"] is True
        assert not journal.exists()

    def test_reboot_after_target_start_before_acceptance_stays_inactive(self, tmp_path):
        env = seed_installed_environment(
            tmp_path,
            installed_source_sha="c" * 40,
            scenario_overrides={"kill_updater_after_target_start": True},
        )
        target = build_update_target_checkout(tmp_path / "target-reboot-c", REPO_ROOT)

        interrupted = _run(env.env, _base_args(target))
        assert interrupted.returncode == -9
        mid = env.state()["vmids"][FAKE_VMID]
        # The updater is allowed to start the DISABLED unit by hand for
        # target acceptance -- systemd permits exactly that.
        assert mid["service"] == "active"
        assert mid["service_enabled"] is False

        env.simulate_pve_ct_reboot(FAKE_VMID)
        rebooted = env.state()["vmids"][FAKE_VMID]
        assert rebooted["started"] is True
        assert rebooted["service"] == "inactive", (
            "an unaccepted target must not survive a reboot as a running service"
        )

        recovered = _run(env.env, _base_args(target))
        assert recovered.returncode == 0, recovered.stderr
        assert "Phase U2" not in recovered.stderr
        assert env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app/__init__.py").exists()
        assert not env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app/inventory/store.py").exists()
        assert env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/.hubinet-source-commit").strip() == "c" * 40
        post = env.state()["vmids"][FAKE_VMID]
        assert post["service"] == "active"
        assert post["service_enabled"] is True
        assert not _update_state_path(env, FAKE_VMID, "journal").exists()

    def test_disable_that_mutates_then_fails_is_already_armed_and_rolls_back(self, tmp_path):
        # `systemctl disable` really removes boot activation and STILL
        # returns non-zero. Recovery is armed before the request is issued,
        # so the resulting rollback restores the enabled + active old
        # service rather than leaving a permanently disabled installation.
        env = seed_installed_environment(
            tmp_path,
            installed_source_sha="d" * 40,
            scenario_overrides={"fail": ["service_autostart_disable_mutate_then_fail"]},
        )
        target = build_update_target_checkout(tmp_path / "target-disable-mutate-fail", REPO_ROOT)

        result = _run(env.env, _base_args(target))
        assert result.returncode != 0
        assert "rollback complete" in result.stderr
        post = env.state()["vmids"][FAKE_VMID]
        assert post["service_enabled"] is True
        assert post["service"] == "active"
        # Nothing beyond the autostart guard was ever mutated.
        assert env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app/__init__.py").exists()
        assert not env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app/inventory/store.py").exists()
        assert env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/.hubinet-source-commit").strip() == "d" * 40
        assert not list(env.ct_file(FAKE_VMID, "/opt/hubinet-ops").glob("app.rollback-*"))
        assert not _update_state_path(env, FAKE_VMID, "journal").exists()

        # A reboot now finds a fully coherent, enabled installation again.
        env.simulate_pve_ct_reboot(FAKE_VMID)
        assert env.state()["vmids"][FAKE_VMID]["service"] == "active"

    def test_disable_reporting_success_without_disabling_refuses_to_mutate(self, tmp_path):
        # Positive control for the three-valued unit-file-state probe: a
        # zero exit from `systemctl disable` is never proof. The updater
        # must refuse to enter the mutation window while systemd would
        # still boot-activate the unit.
        env = seed_installed_environment(
            tmp_path,
            installed_source_sha="e" * 40,
            scenario_overrides={"fail": ["service_autostart_disable_noop_success"]},
        )
        target = build_update_target_checkout(tmp_path / "target-disable-noop", REPO_ROOT)

        result = _run(env.env, _base_args(target))
        assert result.returncode != 0
        assert "still enabled" in result.stderr
        assert "rollback complete" in result.stderr
        post = env.state()["vmids"][FAKE_VMID]
        assert post["service_enabled"] is True
        assert post["service"] == "active"
        assert not env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app/inventory/store.py").exists()
        assert env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/.hubinet-source-commit").strip() == "e" * 40
        assert not _update_state_path(env, FAKE_VMID, "journal").exists()

    def test_recovery_that_cannot_re_enable_hard_stops_and_retains_state(self, tmp_path):
        env = seed_installed_environment(
            tmp_path,
            installed_source_sha="f" * 40,
            scenario_overrides={"kill_updater_after_move": "mv_live_app_to_rollback"},
        )
        target = build_update_target_checkout(tmp_path / "target-enable-fail", REPO_ROOT)
        interrupted = _run(env.env, _base_args(target))
        assert interrupted.returncode == -9
        env.simulate_pve_ct_reboot(FAKE_VMID)
        assert env.state()["vmids"][FAKE_VMID]["service"] == "inactive"

        scenario = json.loads(env.scenario_path.read_text(encoding="utf-8"))
        scenario["fail"] = ["service_autostart_enable"]
        env.scenario_path.write_text(json.dumps(scenario), encoding="utf-8")

        blocked = _run(env.env, _base_args(target))
        assert blocked.returncode != 0
        assert "ROLLBACK COULD NOT BE COMPLETED" in blocked.stderr
        assert "boot activation re-enabled" in blocked.stderr
        assert "rollback complete" not in blocked.stderr
        assert "Phase U2" not in blocked.stderr
        journal = _update_state_path(env, FAKE_VMID, "journal")
        assert journal.exists()
        assert "state=active" in journal.read_text(encoding="utf-8")
        assert env.state()["vmids"][FAKE_VMID]["service_enabled"] is False
        # Run-owned artifacts are retained for manual recovery, never cleaned.
        assert list(env.ct_file(FAKE_VMID, "/opt/hubinet-ops").glob("app.staged-*"))

    def test_success_path_enable_reporting_success_without_enabling_is_not_accepted(self, tmp_path):
        # The mirror-image positive control on the success path: acceptance
        # passed and the source marker is coherent, but `systemctl enable`
        # only CLAIMS to have restored boot activation. That is never
        # accepted as a completed update, and the resulting rollback cannot
        # prove enablement either -- so it hard stops instead of lying.
        env = seed_installed_environment(
            tmp_path,
            installed_source_sha="1" * 40,
            scenario_overrides={"fail": ["service_autostart_enable_noop_success"]},
        )
        target = build_update_target_checkout(tmp_path / "target-enable-noop", REPO_ROOT)

        result = _run(env.env, _base_args(target))
        assert result.returncode != 0
        assert "in-place update: PASS" not in result.stdout
        assert "ROLLBACK COULD NOT BE COMPLETED" in result.stderr
        journal = _update_state_path(env, FAKE_VMID, "journal")
        assert journal.exists()
        assert env.state()["vmids"][FAKE_VMID]["service_enabled"] is False
        # The pre-update marker is restored -- never a new SHA paired with a
        # rolled-back old application payload.
        assert env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/.hubinet-source-commit").strip() == "1" * 40

    def test_successful_update_ends_enabled_active_and_journal_free(self, tmp_path):
        env = seed_installed_environment(tmp_path, installed_source_sha="2" * 40)
        target = build_update_target_checkout(tmp_path / "target-guard-success", REPO_ROOT)

        result = _run(env.env, _base_args(target))
        assert result.returncode == 0, result.stderr
        post = env.state()["vmids"][FAKE_VMID]
        assert post["service"] == "active"
        assert post["service_enabled"] is True
        assert not _update_state_path(env, FAKE_VMID, "journal").exists()

        # Exact guard ordering: disable is the FIRST mutation (before the
        # service stop), the target is started and accepted while still
        # disabled, and enablement is restored only after the installed-
        # source marker has been activated.
        stderr = result.stderr
        disable_at = stderr.index("systemctl disable hubinet-ops")
        stop_at = stderr.index("systemctl stop hubinet-ops")
        start_at = stderr.index("systemctl start hubinet-ops")
        acceptance_at = stderr.index("Phase U5")
        marker_at = stderr.rindex(".hubinet-source-commit")
        enable_at = stderr.index("systemctl enable hubinet-ops")
        assert disable_at < stop_at < start_at < acceptance_at < marker_at < enable_at

        # And a restart of the finished installation brings Hubinet back.
        env.simulate_pve_ct_reboot(FAKE_VMID)
        assert env.state()["vmids"][FAKE_VMID]["service"] == "active"


# ---------------------------------------------------------------------------
# W. Correction pass 7, P1 -- reboot recovery must not depend on the
#    container's /tmp surviving.
#
#    The run-owned authority helper
#    (/tmp/hubinet-ops-authority-tool-<UPDATE_RUN_ID>.py) is pushed exactly
#    once, during the ORIGINAL invocation's Phase U2. A real PVE/CT restart
#    legitimately clears a container's volatile /tmp, and startup recovery
#    deliberately never starts a new plan -- so nothing would re-push it,
#    while every remaining recovery operation (the three-valued path-state
#    probes and the fail-closed authority-database remove/restore) runs
#    THROUGH that helper. The fix is not to pretend /tmp is durable: it is
#    for recovery to re-push the same bounded updater-owned tool to the
#    same reconstructed run-owned path and positively prove it usable
#    before entering rollback.
#
#    These tests are only meaningful because the fake was corrected in the
#    same pass: FakePveEnvironment.simulate_pve_ct_reboot now really
#    empties the CT's /tmp, and the fake `python3` dispatcher refuses to
#    invent execution of a script that is not present in the fake CT
#    filesystem (returning 2, exactly like real CPython).
# ---------------------------------------------------------------------------


class TestRebootTmpLossRecoveryP1:
    def test_reboot_clearing_ct_tmp_repushes_run_owned_tool_then_rolls_back(self, tmp_path):
        env = seed_installed_environment(
            tmp_path,
            installed_source_sha="3" * 40,
            scenario_overrides={"kill_updater_after_move": "mv_live_app_to_rollback"},
        )
        target = build_update_target_checkout(tmp_path / "target-tmp-loss", REPO_ROOT)

        interrupted = _run(env.env, _base_args(target))
        assert interrupted.returncode == -9
        journal = _update_state_path(env, FAKE_VMID, "journal")
        journal_text = journal.read_text(encoding="utf-8")
        assert "rollback_armed=1" in journal_text
        run_id = _journal_run_id(journal_text)

        tool_ct_path = env.ct_file(FAKE_VMID, f"/tmp/hubinet-ops-authority-tool-{run_id}.py")
        assert tool_ct_path.is_file(), (
            "the original invocation must have pushed the run-owned authority helper"
        )

        env.simulate_pve_ct_reboot(FAKE_VMID)
        # The exact defect witness: the helper every rollback operation runs
        # through is GONE, because a restarted container's /tmp is volatile.
        assert not tool_ct_path.exists()
        assert not list(env.ct_file(FAKE_VMID, "/tmp").glob("hubinet-ops-*"))
        # Everything durable survived, which is what recovery still needs.
        assert list(env.ct_file(FAKE_VMID, "/opt/hubinet-ops").glob("app.rollback-*"))
        assert journal.exists()
        assert env.state()["vmids"][FAKE_VMID]["service"] == "inactive"

        before_recovery = len(env.log_lines())
        recovered = _run(env.env, _base_args(target))
        assert recovered.returncode == 0, recovered.stderr
        assert "restored the run-owned authority helper" in recovered.stderr
        assert "rollback complete" in recovered.stderr
        assert "Phase U2" not in recovered.stderr, "recovery must not start a new plan"
        # Exactly one push, and it is the authority helper at the SAME
        # reconstructed run-id path -- never the pre-update HTTP probe,
        # which recovery does not use.
        _assert_recovery_repushed_only_the_authority_tool(env, before_recovery, run_id)
        assert not any(
            "hubinet-ops-update-probe" in line
            for line in env.log_lines()[before_recovery:]
            if line.startswith("pct push")
        )

        # The pre-update installation is coherent, enabled, active, healthy.
        assert env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app/__init__.py").exists()
        assert not env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app/inventory/store.py").exists()
        assert env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/.hubinet-source-commit").strip() == "3" * 40
        post = env.state()["vmids"][FAKE_VMID]
        assert post["service"] == "active"
        assert post["service_enabled"] is True
        assert not journal.exists()
        # The re-pushed recovery helper is itself run-owned and cleaned up.
        assert not tool_ct_path.exists()

    def test_destructive_reset_reboot_recovery_restores_tool_for_db_rollback(self, tmp_path):
        # The same /tmp loss, but on the path where the re-pushed helper is
        # load-bearing for MORE than path probes: a destructive authority
        # reset already happened, so rollback must run the fail-closed
        # `remove` + validated-backup restore through that helper.
        new_backend_instance_id = "22222222-2222-4222-8222-222222222222"
        env = seed_installed_environment(
            tmp_path,
            schema_version=9,
            installed_source_sha="4" * 40,
            scenario_overrides={
                "discovery_backend_instance_id": new_backend_instance_id,
                "kill_updater_after_target_start": True,
            },
        )
        target = build_update_target_checkout(
            tmp_path / "target-reset-tmp-loss", REPO_ROOT, schema_version=10
        )

        interrupted = _run(env.env, _base_args(target, extra=["--allow-authority-reset"]))
        assert interrupted.returncode == -9
        journal = _update_state_path(env, FAKE_VMID, "journal")
        journal_text = journal.read_text(encoding="utf-8")
        assert "ledger=update-authority-reset-attempted" in journal_text
        assert "authority_action=reset_required" in journal_text
        run_id = _journal_run_id(journal_text)
        # The live database really was destroyed after its validated backup.
        assert not env.ct_file(FAKE_VMID, "/var/lib/hubinet-ops/authority.db").exists()
        backup = env.ct_file(
            FAKE_VMID, f"/var/lib/hubinet-ops/update-backups/{run_id}/authority.db"
        )
        assert backup.is_file()

        env.simulate_pve_ct_reboot(FAKE_VMID)
        assert not env.ct_file(
            FAKE_VMID, f"/tmp/hubinet-ops-authority-tool-{run_id}.py"
        ).exists()
        # The authority backup is NOT in /tmp and must survive the restart.
        assert backup.is_file()

        before_recovery = len(env.log_lines())
        recovered = _run(env.env, _base_args(target, extra=["--allow-authority-reset"]))
        assert recovered.returncode == 0, recovered.stderr
        assert "restored the run-owned authority helper" in recovered.stderr
        assert "rollback complete" in recovered.stderr
        assert "Phase U2" not in recovered.stderr
        _assert_recovery_repushed_only_the_authority_tool(env, before_recovery, run_id)

        restored = json.loads(env.ct_file_text(FAKE_VMID, "/var/lib/hubinet-ops/authority.db"))
        assert restored["schema_version"] == 9, (
            "the re-pushed helper must have driven the fail-closed remove + "
            "validated-backup restore, not been silently skipped"
        )
        assert restored["backend_instance_id"] == FAKE_BACKEND_INSTANCE_ID
        assert not env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app/inventory/store.py").exists()
        post = env.state()["vmids"][FAKE_VMID]
        assert post["service"] == "active"
        assert post["service_enabled"] is True
        assert not journal.exists()

    def test_recovery_that_cannot_restore_the_tool_hard_stops_and_preserves_state(self, tmp_path):
        # Fail-closed control: if the run-owned helper cannot be restored,
        # recovery must stop before ANY rollback/path-state/authority
        # operation, preserve the journal and every rollback artifact, and
        # start no new plan.
        env = seed_installed_environment(
            tmp_path,
            installed_source_sha="5" * 40,
            scenario_overrides={"kill_updater_after_move": "mv_live_app_to_rollback"},
        )
        target = build_update_target_checkout(tmp_path / "target-tool-lost", REPO_ROOT)

        interrupted = _run(env.env, _base_args(target))
        assert interrupted.returncode == -9
        env.simulate_pve_ct_reboot(FAKE_VMID)

        scenario = json.loads(env.scenario_path.read_text(encoding="utf-8"))
        scenario["fail"] = ["pct_push"]
        env.scenario_path.write_text(json.dumps(scenario), encoding="utf-8")

        blocked = _run(env.env, _base_args(target))
        assert blocked.returncode != 0
        assert "ROLLBACK COULD NOT BE COMPLETED" in blocked.stderr
        assert "could not restore the run-owned authority helper" in blocked.stderr
        assert "no rollback, path-state, or authority-database operation was attempted" in blocked.stderr
        assert "rollback complete" not in blocked.stderr
        assert "Phase U2" not in blocked.stderr
        journal = _update_state_path(env, FAKE_VMID, "journal")
        assert journal.exists()
        assert "state=active" in journal.read_text(encoding="utf-8")
        # Nothing was touched: no live app restored, artifacts all retained.
        assert not env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app").exists()
        assert list(env.ct_file(FAKE_VMID, "/opt/hubinet-ops").glob("app.rollback-*"))
        assert list(env.ct_file(FAKE_VMID, "/opt/hubinet-ops").glob("app.staged-*"))


# ---------------------------------------------------------------------------
# X. Correction pass 7, P2-A -- rollback must be REPLAYABLE after a partial
#    rollback.
#
#    A first rollback can legitimately restore several artifacts and then
#    hard stop at a LATER terminal operation (re-enable / start / the
#    health proof). That path deliberately RETAINS the active journal, so
#    a later updater invocation re-enters the SAME rollback for the SAME
#    run id. Most rollback helpers already tolerated already-restored
#    state by inspecting the bounded set of paths their artifact owns; the
#    PVE host helper and the systemd unit did not, and diagnosed
#    corruption on state a previous rollback of the same run had itself
#    produced.
#
#    Each test below drives exactly that sequence: update changes the
#    artifact, the target fails, the first rollback restores it and then
#    hard stops at re-enable, the injected terminal problem is fixed, and
#    the updater is invoked again.
# ---------------------------------------------------------------------------


OLD_HELPER_TEXT = "#!/usr/bin/env python3\n# pre-update helper\n"
NEW_HELPER_TEXT = "#!/usr/bin/env python3\n# target helper\n"
OLD_UNIT_TEXT = "[Unit]\nDescription=pre-update unit\n"
NEW_UNIT_TEXT = "[Unit]\nDescription=target unit\n"


def _helper_host_path(env, suffix=""):
    return (
        Path(env.env["HUBINET_OPS_TEST_HOST_ROOT"])
        / "usr" / "local" / "libexec"
        / f"hubinet-package-scan-helper-{FAKE_RUN_ID}{suffix}"
    )


def _clear_scenario_failures(env):
    scenario = json.loads(env.scenario_path.read_text(encoding="utf-8"))
    scenario["fail"] = []
    scenario["discovery_result"] = "healthy"
    env.scenario_path.write_text(json.dumps(scenario), encoding="utf-8")


class TestPartialRollbackRetryP2A:
    def test_second_recovery_after_partial_rollback_with_changed_helper(self, tmp_path):
        env = seed_installed_environment(
            tmp_path,
            installed_source_sha="6" * 40,
            installed_helper_text=OLD_HELPER_TEXT,
            scenario_overrides={
                # The target is activated, then acceptance fails ...
                "discovery_result": "backend_unreachable",
                # ... and the resulting rollback restores helper/unit/app
                # and only THEN hard stops, at the re-enable proof.
                "fail": ["service_autostart_enable"],
            },
        )
        target = build_update_target_checkout(
            tmp_path / "target-helper-retry", REPO_ROOT, helper_text=NEW_HELPER_TEXT
        )
        helper_live = _helper_host_path(env)
        helper_rollback_glob = list(helper_live.parent.glob(f"{helper_live.name}.rollback-*"))
        assert not helper_rollback_glob

        first = _run(env.env, _base_args(target))
        assert first.returncode != 0
        assert "ROLLBACK COULD NOT BE COMPLETED" in first.stderr
        assert "boot activation re-enabled" in first.stderr
        assert "rollback complete" not in first.stderr
        # The helper WAS restored by that first, partial rollback ...
        assert helper_live.read_text(encoding="utf-8") == OLD_HELPER_TEXT
        journal = _update_state_path(env, FAKE_VMID, "journal")
        assert journal.exists()
        journal_text = journal.read_text(encoding="utf-8")
        assert "state=active" in journal_text
        assert "ledger=update-helper-activated" in journal_text
        run_id = _journal_run_id(journal_text)
        # ... and the canonical recovery material was deliberately NOT
        # consumed, so a retry still has the original pre-update helper.
        helper_rollback = _helper_host_path(env, f".rollback-{run_id}")
        assert helper_rollback.is_file()
        assert helper_rollback.read_text(encoding="utf-8") == OLD_HELPER_TEXT
        assert not _helper_host_path(env, f".restore-tmp-{run_id}").exists()

        _clear_scenario_failures(env)
        before_recovery = len(env.log_lines())
        second = _run(env.env, _base_args(target))
        assert second.returncode == 0, second.stderr
        assert "detected prior updater journal" in second.stderr
        assert "rollback complete" in second.stderr
        assert "ROLLBACK COULD NOT BE COMPLETED" not in second.stderr
        assert "Phase U2" not in second.stderr, "recovery must start no new update plan"
        _assert_recovery_repushed_only_the_authority_tool(env, before_recovery, run_id)

        assert helper_live.read_text(encoding="utf-8") == OLD_HELPER_TEXT
        assert not env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app/inventory/store.py").exists()
        assert env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app/__init__.py").exists()
        assert env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/.hubinet-source-commit").strip() == "6" * 40
        post = env.state()["vmids"][FAKE_VMID]
        assert post["service"] == "active"
        assert post["service_enabled"] is True
        assert not journal.exists()
        # Terminal recovery, and only terminal recovery, consumes the
        # run-owned host-side helper artifacts.
        assert not helper_rollback.exists()
        assert not _helper_host_path(env, f".restore-tmp-{run_id}").exists()
        assert not _helper_host_path(env, f".staged-{run_id}").exists()

    def test_second_recovery_with_helper_rollback_artifact_already_consumed(self, tmp_path):
        # The same retry, but starting from the state a rollback that
        # CONSUMED its canonical helper copy leaves behind (what the
        # previous bare `mv` did, and what any already-in-flight run
        # updated from that shape would still present). An absent
        # canonical copy plus a present, executable live helper is a
        # completed restore, never corruption.
        env = seed_installed_environment(
            tmp_path,
            installed_source_sha="7" * 40,
            installed_helper_text=OLD_HELPER_TEXT,
            scenario_overrides={
                "discovery_result": "backend_unreachable",
                "fail": ["service_autostart_enable"],
            },
        )
        target = build_update_target_checkout(
            tmp_path / "target-helper-consumed", REPO_ROOT, helper_text=NEW_HELPER_TEXT
        )

        first = _run(env.env, _base_args(target))
        assert first.returncode != 0
        assert "ROLLBACK COULD NOT BE COMPLETED" in first.stderr
        journal = _update_state_path(env, FAKE_VMID, "journal")
        run_id = _journal_run_id(journal.read_text(encoding="utf-8"))
        helper_live = _helper_host_path(env)
        helper_rollback = _helper_host_path(env, f".rollback-{run_id}")
        assert helper_live.read_text(encoding="utf-8") == OLD_HELPER_TEXT
        helper_rollback.unlink()

        _clear_scenario_failures(env)
        second = _run(env.env, _base_args(target))
        assert second.returncode == 0, second.stderr
        assert "rollback complete" in second.stderr
        assert "ROLLBACK COULD NOT BE COMPLETED" not in second.stderr
        assert "Phase U2" not in second.stderr
        assert helper_live.read_text(encoding="utf-8") == OLD_HELPER_TEXT
        post = env.state()["vmids"][FAKE_VMID]
        assert post["service"] == "active"
        assert post["service_enabled"] is True
        assert not journal.exists()

    def test_retry_whose_live_helper_is_also_gone_never_reaches_a_tolerant_restore(
        self, tmp_path
    ):
        # Positive control for the tolerant branch above: tolerating a
        # consumed canonical copy must never become "assume it is fine".
        # With BOTH the canonical copy and the live helper gone, the
        # forced-command ownership chain no longer identifies this
        # installation at all, so recovery fails closed even earlier --
        # before any rollback, path-state or authority operation -- and
        # preserves the journal and every rollback artifact rather than
        # silently continuing. (_update_rollback_host_helper's own absent
        # + unusable-live hard stop stays as defence in depth behind that
        # gate.)
        env = seed_installed_environment(
            tmp_path,
            installed_source_sha="8" * 40,
            installed_helper_text=OLD_HELPER_TEXT,
            scenario_overrides={
                "discovery_result": "backend_unreachable",
                "fail": ["service_autostart_enable"],
            },
        )
        target = build_update_target_checkout(
            tmp_path / "target-helper-lost", REPO_ROOT, helper_text=NEW_HELPER_TEXT
        )

        first = _run(env.env, _base_args(target))
        assert first.returncode != 0
        journal = _update_state_path(env, FAKE_VMID, "journal")
        run_id = _journal_run_id(journal.read_text(encoding="utf-8"))
        _helper_host_path(env, f".rollback-{run_id}").unlink()
        _helper_host_path(env).unlink()

        _clear_scenario_failures(env)
        before_recovery = len(env.log_lines())
        second = _run(env.env, _base_args(target))
        assert second.returncode != 0
        assert "ownership verification failed" in second.stderr
        assert "does not exist on this PVE host" in second.stderr
        assert "rollback complete" not in second.stderr
        assert "Phase U2" not in second.stderr
        assert journal.exists()
        assert "state=active" in journal.read_text(encoding="utf-8")
        # Nothing was pushed and nothing was restored.
        _assert_recovery_pushed_nothing(env, before_recovery)
        assert env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app/__init__.py").exists()

    def test_second_recovery_after_partial_rollback_with_changed_unit(self, tmp_path):
        env = seed_installed_environment(
            tmp_path,
            installed_source_sha="9" * 40,
            installed_unit_text=OLD_UNIT_TEXT,
            scenario_overrides={
                "discovery_result": "backend_unreachable",
                # PR #65 correction pass 13, P2: the unit changed, so
                # rollback's own boot-enable step now uses `reenable` (to
                # reset any stale target-only link), not plain `enable`.
                "fail": ["service_autostart_reenable"],
            },
        )
        target = build_update_target_checkout(
            tmp_path / "target-unit-retry", REPO_ROOT, unit_text=NEW_UNIT_TEXT
        )
        unit_live = env.ct_file(FAKE_VMID, "/etc/systemd/system/hubinet-ops.service")

        first = _run(env.env, _base_args(target))
        assert first.returncode != 0
        assert "ROLLBACK COULD NOT BE COMPLETED" in first.stderr
        assert "boot activation re-enabled" in first.stderr
        assert "rollback complete" not in first.stderr
        # The unit WAS restored by that first, partial rollback, and its
        # rollback artifact was consumed by the atomic restore rename.
        assert unit_live.read_text(encoding="utf-8") == OLD_UNIT_TEXT
        assert not list(unit_live.parent.glob("hubinet-ops.service.rollback-*"))
        journal = _update_state_path(env, FAKE_VMID, "journal")
        assert journal.exists()
        journal_text = journal.read_text(encoding="utf-8")
        assert "ledger=update-unit-activation-attempted" in journal_text
        run_id = _journal_run_id(journal_text)
        reloads_after_first = env.state()["daemon_reload_calls"]

        _clear_scenario_failures(env)
        before_recovery = len(env.log_lines())
        second = _run(env.env, _base_args(target))
        assert second.returncode == 0, second.stderr
        assert "rollback complete" in second.stderr
        assert "ROLLBACK COULD NOT BE COMPLETED" not in second.stderr
        assert "rollback artifact is absent" not in second.stderr
        assert "Phase U2" not in second.stderr, "recovery must start no new update plan"
        _assert_recovery_repushed_only_the_authority_tool(env, before_recovery, run_id)

        assert unit_live.read_text(encoding="utf-8") == OLD_UNIT_TEXT
        # Correction pass 8 (P2): the replay finds the rollback artifact
        # already consumed and the old unit already on the live path -- and
        # STILL reloads systemd before starting it. "The file is back" is a
        # fact about the filesystem, never proof that the systemd manager
        # stopped holding the target definition (the first rollback here
        # did reload, but a run SIGKILLed between the restore rename and
        # its reload would not have -- see
        # TestUnitRollbackReplayReloadsSystemdP2).
        assert env.state()["daemon_reload_calls"] == reloads_after_first + 1
        assert not env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app/inventory/store.py").exists()
        assert env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app/__init__.py").exists()
        post = env.state()["vmids"][FAKE_VMID]
        assert post["service"] == "active"
        assert post["service_enabled"] is True
        assert not journal.exists()

    def test_unit_rollback_artifact_absent_and_live_unit_gone_still_fails_closed(self, tmp_path):
        # Positive control for the unit's tolerant branch: an absent
        # rollback artifact is only benign while the live unit positively
        # exists. UNKNOWN/absent still fails closed.
        env = seed_installed_environment(
            tmp_path,
            installed_source_sha="a" * 40,
            installed_unit_text=OLD_UNIT_TEXT,
            scenario_overrides={
                "discovery_result": "backend_unreachable",
                # PR #65 correction pass 13, P2: the unit changed, so
                # rollback's own boot-enable step now uses `reenable` (to
                # reset any stale target-only link), not plain `enable`.
                "fail": ["service_autostart_reenable"],
            },
        )
        target = build_update_target_checkout(
            tmp_path / "target-unit-lost", REPO_ROOT, unit_text=NEW_UNIT_TEXT
        )
        first = _run(env.env, _base_args(target))
        assert first.returncode != 0
        journal = _update_state_path(env, FAKE_VMID, "journal")
        assert journal.exists()
        env.ct_file(FAKE_VMID, "/etc/systemd/system/hubinet-ops.service").unlink()

        _clear_scenario_failures(env)
        second = _run(env.env, _base_args(target))
        assert second.returncode != 0
        assert "ROLLBACK COULD NOT BE COMPLETED" in second.stderr
        assert "rollback artifact is absent and the live unit is absent" in second.stderr
        assert "rollback complete" not in second.stderr
        assert "Phase U2" not in second.stderr
        assert journal.exists()


# ---------------------------------------------------------------------------
# Y. Correction pass 7, P2-B -- an UNKNOWN installed-source-marker
#    precondition must never arm a marker mutation that cannot have
#    happened.
#
#    _update_write_source_marker used to record
#    update-marker-activation-attempted BEFORE probing whether an old
#    marker existed. An UNKNOWN probe then died with "attempted" recorded
#    and neither precondition-exists nor precondition-absent, and rollback
#    correctly refused to guess -- hard stopping an otherwise perfectly
#    ordinary full artifact rollback over a marker mutation that provably
#    never occurred. The probe now runs first, and the attempted marker is
#    journaled durably together with its proven precondition, immediately
#    before the first marker `mv`.
# ---------------------------------------------------------------------------


class TestMarkerPreconditionUnknownP2B:
    def test_unknown_marker_precondition_rolls_back_fully_and_leaves_marker_intact(
        self, tmp_path
    ):
        # A fully activated and accepted target -- app, venv/requirements,
        # unit, PVE helper and a destructive authority reset all done --
        # that fails only at the very last step, because the live marker's
        # path state cannot be proven either way.
        new_backend_instance_id = "33333333-3333-4333-8333-333333333333"
        env = seed_installed_environment(
            tmp_path,
            schema_version=9,
            installed_source_sha="b" * 40,
            installed_requirements="fastapi==0.100.0\n",
            installed_unit_text=OLD_UNIT_TEXT,
            installed_helper_text=OLD_HELPER_TEXT,
            scenario_overrides={
                "discovery_backend_instance_id": new_backend_instance_id,
                "path_probe_transport_fail_prefixes": [
                    "/opt/hubinet-ops/.hubinet-source-commit"
                ],
            },
        )
        target = build_update_target_checkout(
            tmp_path / "target-marker-unknown",
            REPO_ROOT,
            schema_version=10,
            requirements_text="fastapi==0.116.1\n",
            unit_text=NEW_UNIT_TEXT,
            helper_text=NEW_HELPER_TEXT,
        )

        result = _run(env.env, _base_args(target, extra=["--allow-authority-reset"]))
        assert result.returncode != 0
        assert "could not prove whether the pre-update installed-source marker exists" in result.stderr
        # The whole point: an UNKNOWN precondition never armed a marker
        # mutation, so rollback is an ordinary full artifact rollback and
        # never hard stops on the marker.
        assert "rollback complete" in result.stderr
        assert "ROLLBACK COULD NOT BE COMPLETED" not in result.stderr
        assert "installed-source marker activation was attempted" not in result.stderr

        # The old marker is exactly unchanged -- never moved aside, never
        # replaced, never removed.
        assert env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/.hubinet-source-commit") == "b" * 40 + "\n"
        assert not list(
            env.ct_file(FAKE_VMID, "/opt/hubinet-ops").glob(".hubinet-source-commit.rollback-*")
        )
        # Every other artifact was restored to its pre-update state.
        assert env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app/__init__.py").exists()
        assert not env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app/inventory/store.py").exists()
        assert env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/requirements.txt") == "fastapi==0.100.0\n"
        assert env.ct_file_text(FAKE_VMID, "/etc/systemd/system/hubinet-ops.service") == OLD_UNIT_TEXT
        assert _helper_host_path(env).read_text(encoding="utf-8") == OLD_HELPER_TEXT
        restored = json.loads(env.ct_file_text(FAKE_VMID, "/var/lib/hubinet-ops/authority.db"))
        assert restored["schema_version"] == 9
        assert restored["backend_instance_id"] == FAKE_BACKEND_INSTANCE_ID
        # And the pre-update installation is coherently back in service.
        post = env.state()["vmids"][FAKE_VMID]
        assert post["service"] == "active"
        assert post["service_enabled"] is True
        assert not _update_state_path(env, FAKE_VMID, "journal").exists()

    def test_marker_precondition_exists_is_journaled_before_the_first_marker_move(
        self, tmp_path
    ):
        # Positive control, old marker EXISTS: the durable journal must
        # carry BOTH the proven precondition and the attempted marker
        # before the first destructive marker `mv` -- witnessed by killing
        # the updater exactly on that move.
        env = seed_installed_environment(
            tmp_path,
            installed_source_sha="c" * 40,
            scenario_overrides={"kill_updater_after_move": "mv_live_marker_to_rollback"},
        )
        target = build_update_target_checkout(tmp_path / "target-marker-exists", REPO_ROOT)

        interrupted = _run(env.env, _base_args(target))
        assert interrupted.returncode == -9
        journal_text = _update_state_path(env, FAKE_VMID, "journal").read_text(encoding="utf-8")
        assert "ledger=update-marker-precondition-exists" in journal_text
        assert "ledger=update-marker-activation-attempted" in journal_text
        assert "ledger=update-marker-precondition-absent" not in journal_text

        recovered = _run(env.env, _base_args(target))
        assert recovered.returncode == 0, recovered.stderr
        assert "Phase U2" not in recovered.stderr
        assert env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/.hubinet-source-commit").strip() == "c" * 40
        post = env.state()["vmids"][FAKE_VMID]
        assert post["service"] == "active"
        assert post["service_enabled"] is True

    def test_marker_precondition_absent_is_journaled_and_leaves_no_marker(self, tmp_path):
        # Positive control, old marker ABSENT: the proven-absent
        # precondition is journaled with the attempted marker, and a
        # failed update must leave NO marker rather than a new SHA paired
        # with the rolled-back old payload.
        env = seed_installed_environment(
            tmp_path,
            installed_source_sha=None,
            scenario_overrides={"fail": ["mv_staged_marker_to_live"]},
        )
        target = build_update_target_checkout(tmp_path / "target-marker-absent", REPO_ROOT)

        result = _run(env.env, _base_args(target))
        assert result.returncode != 0
        assert "rollback complete" in result.stderr
        assert "ROLLBACK COULD NOT BE COMPLETED" not in result.stderr
        assert not env.ct_file(FAKE_VMID, "/opt/hubinet-ops/.hubinet-source-commit").exists()
        assert not env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app/inventory/store.py").exists()
        post = env.state()["vmids"][FAKE_VMID]
        assert post["service"] == "active"
        assert post["service_enabled"] is True


# ---------------------------------------------------------------------------
# Correction pass 8, P1 -- a changed-requirements update BUILDS the target
# virtualenv at its FINAL live pathname.
#
# The rejected design built /opt/hubinet-ops/.venv.staged-<runid> while the
# old service was still running and then renamed that directory onto
# /opt/hubinet-ops/.venv. A Python virtualenv is not generally relocatable:
# the console entrypoints pip/ensurepip generate embed the ABSOLUTE
# interpreter path of the environment they were created in, so the
# "activated" environment's own bin/pip still pointed at a staging pathname
# that no longer existed. The fake reproduces exactly that property (it
# writes the build path into the generated entrypoint), so these tests fail
# against the old design. The real, non-faked half of this proof -- a
# genuine stdlib venv whose generated console script is inspected, and
# whose shebang demonstrably does NOT follow a rename -- lives in
# tests/test_update_authority_helpers.py.
# ---------------------------------------------------------------------------


def _venv_build_invocations(env):
    return [
        line
        for line in env.log_lines()
        if " python3 /tmp/hubinet-ops-update-venv-stage.py" in line
    ]


class TestVenvBuiltAtFinalPathP1:
    def test_changed_requirements_build_the_venv_at_the_final_live_path(self, tmp_path):
        env = seed_installed_environment(tmp_path, installed_requirements="fastapi==0.100.0\n")
        target = build_update_target_checkout(
            tmp_path / "target-final-venv", REPO_ROOT, requirements_text="fastapi==0.116.1\n"
        )
        result = _run(env.env, _base_args(target))
        assert result.returncode == 0, result.stderr

        # The build helper is invoked exactly once, with the FINAL live
        # pathname as its destination -- never a staging pathname.
        builds = _venv_build_invocations(env)
        assert len(builds) == 1, builds
        assert " /opt/hubinet-ops/.venv " in builds[0], builds
        assert ".venv.staged-" not in builds[0], builds

        # No staged-venv path is created anywhere, at any point in the run.
        assert not any(".venv.staged-" in line for line in env.log_lines())
        assert not list(env.ct_file(FAKE_VMID, "/opt/hubinet-ops").glob(".venv.staged-*"))
        assert not list(env.ct_file(FAKE_VMID, "/opt/hubinet-ops").glob(".venv.rollback-*"))

        # The activated environment's own generated entrypoint carries the
        # absolute path it was built at. Under the rejected staged-then-
        # rename design this would name a .venv.staged-<runid> directory
        # that no longer exists.
        pip_text = env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/.venv/bin/pip")
        assert pip_text.strip() == "#!/opt/hubinet-ops/.venv/bin/python", pip_text
        assert env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/requirements.txt") == "fastapi==0.116.1\n"

    def test_code_only_update_never_builds_or_touches_the_venv(self, tmp_path):
        """Negative control: requirements unchanged -> no venv work at all."""
        env = seed_installed_environment(tmp_path, installed_requirements="fastapi==0.116.1\n")
        pre_pip = env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/.venv/bin/pip")
        pre_requirements = env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/requirements.txt")
        target = build_update_target_checkout(
            tmp_path / "target-code-only-venv", REPO_ROOT, requirements_text="fastapi==0.116.1\n"
        )
        result = _run(env.env, _base_args(target))
        assert result.returncode == 0, result.stderr

        assert not any(
            "hubinet-ops-update-venv-stage.py" in line for line in env.log_lines()
        ), "a code-only update must neither push nor run the venv build helper"
        assert env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/.venv/bin/pip") == pre_pip
        assert env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/requirements.txt") == pre_requirements
        assert not list(env.ct_file(FAKE_VMID, "/opt/hubinet-ops").glob(".venv.rollback-*"))

    def test_pip_install_failure_rolls_back_to_the_old_environment(self, tmp_path):
        """A failed build at the final path leaves the OLD installation coherent."""
        env = seed_installed_environment(
            tmp_path,
            installed_requirements="fastapi==0.100.0\n",
            scenario_overrides={"fail": ["pip_install"]},
        )
        target = build_update_target_checkout(
            tmp_path / "target-venv-build-fail", REPO_ROOT, requirements_text="fastapi==0.116.1\n"
        )
        result = _run(env.env, _base_args(target))
        assert result.returncode != 0
        assert "rollback complete" in result.stderr

        # The partial target environment is gone and the preserved old one
        # is back at the live path, with its own original entrypoint.
        assert env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/.venv/bin/pip") == "#!/bin/sh\n"
        assert env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/requirements.txt") == "fastapi==0.100.0\n"
        assert not list(env.ct_file(FAKE_VMID, "/opt/hubinet-ops").glob(".venv.rollback-*"))
        assert not env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app/inventory/store.py").exists()
        assert env.state()["vmids"][FAKE_VMID]["service"] == "active"
        assert env.state()["vmids"][FAKE_VMID]["service_enabled"] is True
        assert not _update_state_path(env, FAKE_VMID, "journal").exists()

    def test_venv_create_failure_partial_target_is_removed_by_rollback(self, tmp_path):
        """The build can die with a half-created directory at the LIVE path."""
        env = seed_installed_environment(
            tmp_path,
            installed_requirements="fastapi==0.100.0\n",
            scenario_overrides={"fail": ["venv_create"]},
        )
        target = build_update_target_checkout(
            tmp_path / "target-venv-create-fail", REPO_ROOT, requirements_text="fastapi==0.116.1\n"
        )
        result = _run(env.env, _base_args(target))
        assert result.returncode != 0
        assert "rollback complete" in result.stderr
        assert env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/.venv/bin/pip") == "#!/bin/sh\n"
        assert env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/requirements.txt") == "fastapi==0.100.0\n"
        assert env.state()["vmids"][FAKE_VMID]["service"] == "active"

    def test_sigkill_during_final_path_build_is_recovered_on_the_next_run(self, tmp_path):
        env = seed_installed_environment(
            tmp_path,
            installed_requirements="fastapi==0.100.0\n",
            installed_source_sha="9" * 40,
            scenario_overrides={"kill_updater_during_venv_build": True},
        )
        target = build_update_target_checkout(
            tmp_path / "target-venv-sigkill", REPO_ROOT, requirements_text="fastapi==0.116.1\n"
        )
        interrupted = _run(env.env, _base_args(target))
        assert interrupted.returncode == -9

        journal = _update_state_path(env, FAKE_VMID, "journal")
        journal_text = journal.read_text(encoding="utf-8")
        assert "state=active" in journal_text
        assert "rollback_armed=1" in journal_text
        assert "ledger=update-venv-activation-attempted" in journal_text
        # The interrupted build really was at the FINAL live path, and the
        # old environment is preserved under this run's rollback name.
        assert (
            env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/.venv/bin/pip").strip()
            == "#!/opt/hubinet-ops/.venv/bin/python"
        )
        assert list(env.ct_file(FAKE_VMID, "/opt/hubinet-ops").glob(".venv.rollback-*"))
        assert env.state()["vmids"][FAKE_VMID]["service_enabled"] is False

        scenario = json.loads(env.scenario_path.read_text(encoding="utf-8"))
        scenario.pop("kill_updater_during_venv_build", None)
        env.scenario_path.write_text(json.dumps(scenario), encoding="utf-8")

        recovered = _run(env.env, _base_args(target))
        assert recovered.returncode == 0, recovered.stderr
        assert "previous interrupted update" in recovered.stderr
        assert "Phase U2" not in recovered.stderr
        # A partially built environment is never resumed: it is removed and
        # the preserved pre-update environment is restored verbatim.
        assert env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/.venv/bin/pip") == "#!/bin/sh\n"
        assert env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/requirements.txt") == "fastapi==0.100.0\n"
        assert not list(env.ct_file(FAKE_VMID, "/opt/hubinet-ops").glob(".venv.rollback-*"))
        assert env.state()["vmids"][FAKE_VMID]["service"] == "active"
        assert env.state()["vmids"][FAKE_VMID]["service_enabled"] is True
        assert not journal.exists()


# ---------------------------------------------------------------------------
# Correction pass 8, P2 -- rollback readiness is a bounded POLL, not a
# one-shot request. hubinet-ops.service is Type=simple, so systemd reports
# `active` as soon as the process is exec'd -- strictly earlier than the
# moment uvicorn has bound 127.0.0.1:8787. A single health request fired at
# that instant misclassified an ordinary startup race as a failed rollback.
# ---------------------------------------------------------------------------


class TestRollbackHealthReadinessP2:
    def test_delayed_health_readiness_still_completes_the_rollback(self, tmp_path):
        env = seed_installed_environment(
            tmp_path,
            scenario_overrides={
                "discovery_result": "backend_unreachable",
                # Deterministic probe counter, never a wall-clock sleep:
                # the first three health requests of the run get no answer,
                # exactly as they would while uvicorn is still binding
                # 127.0.0.1:8787 behind an already-`active` Type=simple unit.
                "health_fail_first_n": 3,
            },
        )
        # Comfortably inside the EXISTING bounded startup/service deadline
        # (the fake environment's default is deliberately tiny) so the
        # retries have somewhere to happen. The one-shot probe this
        # replaces would fail here no matter how long the deadline is.
        env.env["BOOTSTRAP_SERVICE_TIMEOUT_SECONDS"] = "10"
        target = build_update_target_checkout(tmp_path / "target-slow-health", REPO_ROOT)
        result = _run(env.env, _base_args(target))

        # The UPDATE still failed (that is what triggered the rollback),
        # but the ROLLBACK itself must succeed.
        assert result.returncode != 0
        assert "rollback complete" in result.stderr
        assert "ROLLBACK COULD NOT BE COMPLETED" not in result.stderr
        assert env.state()["vmids"][FAKE_VMID]["service"] == "active"
        assert env.state()["vmids"][FAKE_VMID]["service_enabled"] is True
        assert not env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app/inventory/store.py").exists()
        assert not _update_state_path(env, FAKE_VMID, "journal").exists()
        assert env.state()["backend_health_calls"] >= 4

    def test_health_never_ready_hard_stops_and_retains_every_artifact(self, tmp_path):
        env = seed_installed_environment(
            tmp_path,
            scenario_overrides={
                "discovery_result": "backend_unreachable",
                "fail": ["backend_health"],
            },
        )
        # Bound the EXISTING startup/service deadline for this test rather
        # than inventing a new one -- the loop is deliberately not allowed
        # to run unbounded.
        env.env["BOOTSTRAP_SERVICE_TIMEOUT_SECONDS"] = "3"
        target = build_update_target_checkout(tmp_path / "target-dead-health", REPO_ROOT)
        result = _run(env.env, _base_args(target))

        assert result.returncode != 0
        assert "ROLLBACK COULD NOT BE COMPLETED" in result.stderr
        assert "unauthenticated health probe" in result.stderr
        journal = _update_state_path(env, FAKE_VMID, "journal")
        assert journal.exists()
        assert "state=active" in journal.read_text(encoding="utf-8")

    def test_recovery_after_a_transient_readiness_failure_completes(self, tmp_path):
        env = seed_installed_environment(
            tmp_path,
            scenario_overrides={
                "discovery_result": "backend_unreachable",
                "fail": ["backend_health"],
            },
        )
        env.env["BOOTSTRAP_SERVICE_TIMEOUT_SECONDS"] = "3"
        target = build_update_target_checkout(tmp_path / "target-transient-health", REPO_ROOT)
        blocked = _run(env.env, _base_args(target))
        assert blocked.returncode != 0
        assert "ROLLBACK COULD NOT BE COMPLETED" in blocked.stderr
        journal = _update_state_path(env, FAKE_VMID, "journal")
        assert journal.exists()

        _clear_scenario_failures(env)
        recovered = _run(env.env, _base_args(target))
        assert recovered.returncode == 0, recovered.stderr
        assert "previous interrupted update" in recovered.stderr
        assert "Phase U2" not in recovered.stderr
        assert env.state()["vmids"][FAKE_VMID]["service"] == "active"
        assert env.state()["vmids"][FAKE_VMID]["service_enabled"] is True
        assert not journal.exists()


# ---------------------------------------------------------------------------
# Correction pass 11, P2 -- target startup readiness has the same bounded
# systemd-active + HTTP-health contract as rollback, before Phase U5.
# ---------------------------------------------------------------------------


class TestForwardTargetHealthReadinessP2:
    def test_delayed_target_health_reaches_phase_u5_and_succeeds(self, tmp_path):
        env = seed_installed_environment(
            tmp_path, scenario_overrides={"health_fail_first_n": 3}
        )
        env.env["BOOTSTRAP_SERVICE_TIMEOUT_SECONDS"] = "6"
        target = build_update_target_checkout(tmp_path / "target-forward-slow-health", REPO_ROOT)
        result = _run(env.env, _base_args(target))

        assert result.returncode == 0, result.stderr
        assert env.state()["backend_health_calls"] == 4
        start_at = result.stderr.index("systemctl start hubinet-ops")
        ready_at = result.stderr.index(
            "target HTTP readiness: systemd active and health endpoint ready"
        )
        acceptance_at = result.stderr.index("Phase U5: acceptance")
        assert start_at < ready_at < acceptance_at

    def test_target_health_timeout_triggers_coherent_rollback(self, tmp_path):
        env = seed_installed_environment(
            tmp_path,
            # Exactly the three target-side probes fail. The first rollback
            # health probe is call four and succeeds deterministically.
            scenario_overrides={"health_fail_first_n": 3},
        )
        env.env["BOOTSTRAP_SERVICE_TIMEOUT_SECONDS"] = "3"
        target = build_update_target_checkout(tmp_path / "target-forward-health-timeout", REPO_ROOT)
        result = _run(env.env, _base_args(target))

        assert result.returncode != 0
        assert "did not prove systemd active AND answer" in result.stderr
        assert "Phase U5: acceptance" not in result.stderr
        assert "rollback complete" in result.stderr
        assert "ROLLBACK COULD NOT BE COMPLETED" not in result.stderr
        assert env.state()["backend_health_calls"] == 4
        assert env.state()["vmids"][FAKE_VMID]["service"] == "active"
        assert env.state()["vmids"][FAKE_VMID]["service_enabled"] is True

    def test_target_systemd_failed_is_terminal_and_never_passes_readiness(self, tmp_path):
        env = seed_installed_environment(
            tmp_path, scenario_overrides={"target_service_failed_after_start": True}
        )
        target = build_update_target_checkout(tmp_path / "target-forward-systemd-failed", REPO_ROOT)
        result = _run(env.env, _base_args(target))

        assert result.returncode != 0
        assert "systemd reports the unit as failed" in result.stderr
        assert "target HTTP readiness: systemd active and health endpoint ready" not in result.stderr
        assert "Phase U5: acceptance" not in result.stderr
        assert "rollback complete" in result.stderr


# ---------------------------------------------------------------------------
# Correction pass 15, P1 -- a BOUNDED readiness loop does not by itself
# bound an UNBOUNDED inner curl call. If the target accepts the TCP
# connection but stalls before sending a usable HTTP response, an
# unbounded curl blocks the outer loop's own deadline check forever.
# Every health request _update_wait_until_service_active_and_healthy
# issues must carry its own `--max-time`, computed as whatever remains of
# the SAME BOOTSTRAP_SERVICE_TIMEOUT_SECONDS budget -- never a second,
# independent timeout.
# ---------------------------------------------------------------------------


class TestBoundedHealthRequestP1:
    def test_production_health_request_always_carries_max_time(self, tmp_path):
        # D: static proof the actual curl invocation is bounded, on an
        # ordinary successful run (both the target and, incidentally, any
        # acceptance-time probe).
        env = seed_installed_environment(tmp_path)
        target = build_update_target_checkout(tmp_path / "target-max-time-present", REPO_ROOT)
        result = _run(env.env, _base_args(target))

        assert result.returncode == 0, result.stderr
        curl_health_lines = [
            line
            for line in env.log_lines()
            if "curl" in line and "/r0/v1/health" in line
        ]
        assert curl_health_lines, "expected at least one health curl invocation"
        assert all("--max-time" in line for line in curl_health_lines)

    def test_target_health_stall_recovers_within_the_same_deadline(self, tmp_path):
        # Delayed-success regression: the first few health requests stall
        # (TCP accepted, no usable response) and are bounded by --max-time;
        # a later request inside the SAME deadline succeeds.
        env = seed_installed_environment(
            tmp_path, scenario_overrides={"health_stall_first_n": 3}
        )
        env.env["BOOTSTRAP_SERVICE_TIMEOUT_SECONDS"] = "10"
        target = build_update_target_checkout(tmp_path / "target-stall-then-ready", REPO_ROOT)
        result = _run(env.env, _base_args(target))

        assert result.returncode == 0, result.stderr
        assert "FAKE-CURL" not in result.stderr
        assert env.state()["backend_health_calls"] == 4
        assert "target HTTP readiness: systemd active and health endpoint ready" in result.stderr

    def test_target_health_stall_past_deadline_is_bounded_and_hard_stops(self, tmp_path):
        # Stalled-request regression: the health endpoint NEVER answers
        # usefully, for either the target OR the restored old service (a
        # persistent network-level stall, not something rolling back code
        # can fix). Without --max-time this would hang the FIRST bounded
        # loop forever; with it, that deadline is reached in bounded time
        # and rollback is attempted -- which then, correctly, also cannot
        # prove the restored old service healthy within ITS OWN bounded
        # deadline, and hard-stops preserving the active journal (exactly
        # test_health_never_ready_hard_stops_and_retains_every_artifact's
        # existing shape). The witness this test exists for is that BOTH
        # bounded waits actually terminate -- neither one hangs the
        # process -- not that rollback can magically fix a stalled network.
        env = seed_installed_environment(
            tmp_path, scenario_overrides={"health_stall_first_n": 999}
        )
        env.env["BOOTSTRAP_SERVICE_TIMEOUT_SECONDS"] = "3"
        target = build_update_target_checkout(tmp_path / "target-stall-forever", REPO_ROOT)
        result = _run(env.env, _base_args(target))

        assert result.returncode != 0
        assert "FAKE-CURL" not in result.stderr
        assert "did not prove systemd active AND answer" in result.stderr
        assert "ROLLBACK COULD NOT BE COMPLETED" in result.stderr
        journal = _update_state_path(env, FAKE_VMID, "journal")
        assert journal.exists()
        assert "state=active" in journal.read_text(encoding="utf-8")

    def test_rollback_health_stall_recovers_within_the_same_deadline(self, tmp_path):
        # Same bounded contract on the ROLLBACK side: forward activation is
        # forced to fail post-start (via acceptance), and the restored old
        # service's own readiness poll must survive a stalled request too.
        env = seed_installed_environment(
            tmp_path,
            scenario_overrides={
                "discovery_result": "backend_unreachable",
                "health_stall_first_n": 3,
            },
        )
        env.env["BOOTSTRAP_SERVICE_TIMEOUT_SECONDS"] = "10"
        target = build_update_target_checkout(tmp_path / "target-rollback-stall-then-ready", REPO_ROOT)
        result = _run(env.env, _base_args(target))

        assert result.returncode != 0
        assert "FAKE-CURL" not in result.stderr
        assert "rollback complete" in result.stderr
        assert "ROLLBACK COULD NOT BE COMPLETED" not in result.stderr
        assert env.state()["backend_health_calls"] >= 4

    # A permanent (never-succeeds) stall necessarily affects the FIRST
    # bounded wait it reaches -- forward target activation, since that
    # runs before Phase U5/acceptance can ever trigger a rollback -- so
    # that bounded-hard-stop witness is test_target_health_stall_past_
    # deadline_is_bounded_and_hard_stops above, not a rollback-specific
    # scenario; this fake's stall counter has no way to target "only the
    # restored old service's own calls" while leaving the target's calls
    # unaffected.


# ---------------------------------------------------------------------------
# Correction pass 8, P2 -- a REPLAYED unit rollback must still reload
# systemd. "The old unit file is back on the live path" is a fact about the
# filesystem and is never proof that the systemd MANAGER stopped holding
# the target definition in memory.
# ---------------------------------------------------------------------------


class TestUnitRollbackReplayReloadsSystemdP2:
    def test_replay_after_unit_restore_still_daemon_reloads_before_start(self, tmp_path):
        original_unit = (REPO_ROOT / "deploy" / "hubinet-ops-0.5.service").read_text(encoding="utf-8")
        env = seed_installed_environment(
            tmp_path,
            scenario_overrides={
                "discovery_result": "backend_unreachable",
                # SIGKILL the updater the instant the preserved old unit is
                # back on the live path -- i.e. strictly before rollback's
                # own daemon-reload.
                "kill_updater_after_move": "mv_rollback_unit_to_live",
            },
        )
        target = build_update_target_checkout(
            tmp_path / "target-unit-replay", REPO_ROOT,
            unit_text="[Unit]\nDescription=changed target unit\n",
        )
        interrupted = _run(env.env, _base_args(target))
        assert interrupted.returncode == -9

        # Exactly one daemon-reload has happened so far: the FORWARD one
        # that loaded the target unit. Rollback's own never ran.
        assert env.state()["daemon_reload_calls"] == 1
        live_unit = env.ct_file_text(FAKE_VMID, "/etc/systemd/system/hubinet-ops.service")
        assert live_unit == original_unit
        assert not list(
            env.ct_file(FAKE_VMID, "/etc/systemd/system").glob("hubinet-ops.service.rollback-*")
        ), "the rollback artifact was consumed by the interrupted restore"
        journal = _update_state_path(env, FAKE_VMID, "journal")
        journal_text = journal.read_text(encoding="utf-8")
        assert "ledger=update-unit-activation-attempted" in journal_text

        scenario = json.loads(env.scenario_path.read_text(encoding="utf-8"))
        scenario.pop("kill_updater_after_move", None)
        env.scenario_path.write_text(json.dumps(scenario), encoding="utf-8")

        before_recovery = len(env.log_lines())
        recovered = _run(env.env, _base_args(target))
        assert recovered.returncode == 0, recovered.stderr
        assert "previous interrupted update" in recovered.stderr
        assert "Phase U2" not in recovered.stderr

        # The replay found no rollback artifact and the old unit already in
        # place -- and still reloaded systemd before starting it.
        assert env.state()["daemon_reload_calls"] == 2
        recovery_lines = env.log_lines()[before_recovery:]
        probe_index = next(
            i for i, line in enumerate(recovery_lines)
            if "path-state" in line and "hubinet-ops.service.rollback-" in line
        )
        reload_index = next(
            i for i, line in enumerate(recovery_lines) if "daemon-reload" in line
        )
        start_index = next(
            i for i, line in enumerate(recovery_lines)
            if line.endswith("systemctl start hubinet-ops")
        )
        assert probe_index < reload_index < start_index, recovery_lines
        assert env.ct_file_text(FAKE_VMID, "/etc/systemd/system/hubinet-ops.service") == original_unit
        assert env.state()["vmids"][FAKE_VMID]["service"] == "active"
        assert not journal.exists()

    def test_replay_with_rollback_artifact_absent_and_no_live_unit_fails_closed(self, tmp_path):
        """Positive control: the tolerant branch is not a blanket pass."""
        env = seed_installed_environment(
            tmp_path,
            scenario_overrides={
                "discovery_result": "backend_unreachable",
                "kill_updater_after_move": "mv_rollback_unit_to_live",
            },
        )
        target = build_update_target_checkout(
            tmp_path / "target-unit-replay-gone", REPO_ROOT,
            unit_text="[Unit]\nDescription=changed target unit\n",
        )
        assert _run(env.env, _base_args(target)).returncode == -9

        scenario = json.loads(env.scenario_path.read_text(encoding="utf-8"))
        scenario.pop("kill_updater_after_move", None)
        env.scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
        env.ct_file(FAKE_VMID, "/etc/systemd/system/hubinet-ops.service").unlink()

        blocked = _run(env.env, _base_args(target))
        assert blocked.returncode != 0
        assert "ROLLBACK COULD NOT BE COMPLETED" in blocked.stderr
        assert _update_state_path(env, FAKE_VMID, "journal").exists()


# ---------------------------------------------------------------------------
# Correction pass 8, P2 -- the durable `update-authority-restored`
# checkpoint. Once the pre-update authority database has been restored and
# that fact is durable, a REPLAYED rollback must never re-apply the original
# backup: the restored old service may legitimately have written new
# authority state since, and re-applying the backup would destroy it.
# ---------------------------------------------------------------------------


def _authority_backup_restores(env):
    return [
        line
        for line in env.log_lines()
        if " cp " in line
        and "/var/lib/hubinet-ops/update-backups/" in line
        and line.endswith("/var/lib/hubinet-ops/authority.db")
    ]


def _interrupt_after_authority_rollback_restart(tmp_path, name):
    """Drive a destructive-reset update to a rollback that restores the old
    authority database, journals `update-authority-restored`, restarts the
    old service -- and is then SIGKILLed before the journal reaches a
    terminal state."""
    env = seed_installed_environment(
        tmp_path,
        schema_version=9,
        scenario_overrides={
            "discovery_result": "backend_unreachable",
            # Start #1 is the target's; start #2 is the ROLLBACK's own
            # restart of the restored old installation.
            "kill_updater_after_service_start_call": 2,
        },
    )
    target = build_update_target_checkout(tmp_path / name, REPO_ROOT, schema_version=10)
    interrupted = _run(env.env, _base_args(target, extra=["--allow-authority-reset"]))
    assert interrupted.returncode == -9

    journal = _update_state_path(env, FAKE_VMID, "journal")
    journal_text = journal.read_text(encoding="utf-8")
    assert "state=active" in journal_text
    assert "ledger=update-authority-reset-attempted" in journal_text
    assert "ledger=update-authority-restored" in journal_text, (
        "the restore checkpoint must be durable BEFORE the old service can start"
    )
    restored = json.loads(env.ct_file_text(FAKE_VMID, "/var/lib/hubinet-ops/authority.db"))
    assert restored["schema_version"] == 9
    assert restored["backend_instance_id"] == FAKE_BACKEND_INSTANCE_ID
    assert len(_authority_backup_restores(env)) == 1

    scenario = json.loads(env.scenario_path.read_text(encoding="utf-8"))
    scenario.pop("kill_updater_after_service_start_call", None)
    env.scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
    return env, target, journal


class TestAuthorityRestoredCheckpointP2:
    def test_post_rollback_authority_writes_survive_a_replayed_rollback(self, tmp_path):
        env, target, journal = _interrupt_after_authority_rollback_restart(
            tmp_path, "target-authority-replay"
        )

        # The restored OLD service is running again and legitimately writes
        # new authority state (discovery, scans, approvals...).
        db_path = env.ct_file(FAKE_VMID, "/var/lib/hubinet-ops/authority.db")
        db = json.loads(db_path.read_text(encoding="utf-8"))
        db["post_rollback_write"] = "committed-run-sequence-99"
        db_path.write_text(json.dumps(db), encoding="utf-8")

        recovered = _run(env.env, _base_args(target, extra=["--allow-authority-reset"]))
        assert recovered.returncode == 0, recovered.stderr
        assert "previous interrupted update" in recovered.stderr
        assert "Phase U2" not in recovered.stderr
        assert "already restored durably" in recovered.stderr

        final = json.loads(db_path.read_text(encoding="utf-8"))
        assert final["post_rollback_write"] == "committed-run-sequence-99", (
            "a replayed rollback must never re-apply the original backup over "
            "writes the restored old service made after the first rollback"
        )
        assert final["schema_version"] == 9
        assert final["backend_instance_id"] == FAKE_BACKEND_INSTANCE_ID
        # Exactly one backup restore across BOTH invocations.
        assert len(_authority_backup_restores(env)) == 1
        assert env.state()["vmids"][FAKE_VMID]["service"] == "active"
        assert env.state()["vmids"][FAKE_VMID]["service_enabled"] is True
        assert not journal.exists()

    def test_replay_with_a_different_live_authority_identity_hard_stops(self, tmp_path):
        env, target, journal = _interrupt_after_authority_rollback_restart(
            tmp_path, "target-authority-wrong-identity"
        )
        db_path = env.ct_file(FAKE_VMID, "/var/lib/hubinet-ops/authority.db")
        db = json.loads(db_path.read_text(encoding="utf-8"))
        db["backend_instance_id"] = "22222222-2222-4222-8222-222222222222"
        db_path.write_text(json.dumps(db), encoding="utf-8")
        backups = list(
            env.ct_file(FAKE_VMID, "/var/lib/hubinet-ops/update-backups").rglob("authority.db")
        )
        assert backups

        blocked = _run(env.env, _base_args(target, extra=["--allow-authority-reset"]))
        assert blocked.returncode != 0
        assert "ROLLBACK COULD NOT BE COMPLETED" in blocked.stderr
        assert "backend_instance_id" in blocked.stderr

        # The uncertain live database is NOT overwritten, and the validated
        # backup is retained for manual diagnosis.
        still_there = json.loads(db_path.read_text(encoding="utf-8"))
        assert still_there["backend_instance_id"] == "22222222-2222-4222-8222-222222222222"
        assert len(_authority_backup_restores(env)) == 1
        assert backups[0].exists()
        assert journal.exists()
        assert "state=active" in journal.read_text(encoding="utf-8")

    def test_replay_with_a_missing_live_authority_hard_stops(self, tmp_path):
        env, target, journal = _interrupt_after_authority_rollback_restart(
            tmp_path, "target-authority-missing"
        )
        env.ct_file(FAKE_VMID, "/var/lib/hubinet-ops/authority.db").unlink()
        backups = list(
            env.ct_file(FAKE_VMID, "/var/lib/hubinet-ops/update-backups").rglob("authority.db")
        )
        assert backups

        blocked = _run(env.env, _base_args(target, extra=["--allow-authority-reset"]))
        assert blocked.returncode != 0
        assert "ROLLBACK COULD NOT BE COMPLETED" in blocked.stderr
        assert "missing, corrupt, or unrecognized" in blocked.stderr
        assert len(_authority_backup_restores(env)) == 1
        assert backups[0].exists()
        assert journal.exists()

    def test_replay_with_a_corrupt_live_authority_hard_stops(self, tmp_path):
        env, target, journal = _interrupt_after_authority_rollback_restart(
            tmp_path, "target-authority-corrupt"
        )
        env.ct_file(FAKE_VMID, "/var/lib/hubinet-ops/authority.db").write_text(
            "not-json-and-not-sqlite", encoding="utf-8"
        )
        blocked = _run(env.env, _base_args(target, extra=["--allow-authority-reset"]))
        assert blocked.returncode != 0
        assert "ROLLBACK COULD NOT BE COMPLETED" in blocked.stderr
        assert "missing, corrupt, or unrecognized" in blocked.stderr
        assert env.ct_file_text(FAKE_VMID, "/var/lib/hubinet-ops/authority.db") == (
            "not-json-and-not-sqlite"
        )
        assert len(_authority_backup_restores(env)) == 1
        assert journal.exists()

    def test_first_authority_rollback_restores_the_backup_exactly_once(self, tmp_path):
        """Positive control: the ordinary, uninterrupted path is unchanged."""
        env = seed_installed_environment(
            tmp_path,
            schema_version=9,
            scenario_overrides={"discovery_result": "backend_unreachable"},
        )
        target = build_update_target_checkout(
            tmp_path / "target-authority-once", REPO_ROOT, schema_version=10
        )
        result = _run(env.env, _base_args(target, extra=["--allow-authority-reset"]))
        assert result.returncode != 0
        assert "rollback complete" in result.stderr
        assert len(_authority_backup_restores(env)) == 1
        db = json.loads(env.ct_file_text(FAKE_VMID, "/var/lib/hubinet-ops/authority.db"))
        assert db["schema_version"] == 9
        assert db["backend_instance_id"] == FAKE_BACKEND_INSTANCE_ID
        assert env.state()["vmids"][FAKE_VMID]["service"] == "active"
        assert not _update_state_path(env, FAKE_VMID, "journal").exists()


# ---------------------------------------------------------------------------
# AA. Correction pass 9, P1 -- filesystem durability barriers.
#
# The durable host journal proves a namespace mv/cp/rm completed in the
# running kernel; it does not by itself prove the state a LATER transition
# depends on would survive a subsequent host power loss. Each forward
# barrier below is the CT/host-side `sync -f` deploy/lib/update-recovery.sh
# now issues before the destructive transition that depends on the
# preceding rollback-preservation move -- see that file's own header
# comment and update-activate.sh's per-artifact call sites.
# ---------------------------------------------------------------------------


class TestForwardDurabilityBarrierOrdering:
    def test_app_barrier_crosses_between_preservation_and_activation(self, tmp_path):
        env = seed_installed_environment(tmp_path)
        target = build_update_target_checkout(tmp_path / "target-barrier-app-order", REPO_ROOT)
        result = _run(env.env, _base_args(target))
        assert result.returncode == 0, result.stderr
        stderr = result.stderr
        preserve_at = stderr.index("-- mv /opt/hubinet-ops/app /opt/hubinet-ops/app.rollback-")
        barrier_at = stderr.index("-- sync -f /opt/hubinet-ops/app.rollback-")
        activate_at = stderr.index("-- mv /opt/hubinet-ops/app.staged-")
        assert preserve_at < barrier_at < activate_at

    def test_venv_barrier_crosses_between_preservation_and_final_path_build(self, tmp_path):
        env = seed_installed_environment(tmp_path, installed_requirements="fastapi==0.100.0\n")
        target = build_update_target_checkout(
            tmp_path / "target-barrier-venv-order", REPO_ROOT, requirements_text="fastapi==0.116.1\n"
        )
        result = _run(env.env, _base_args(target))
        assert result.returncode == 0, result.stderr
        stderr = result.stderr
        preserve_at = stderr.index("-- mv /opt/hubinet-ops/.venv /opt/hubinet-ops/.venv.rollback-")
        barrier_at = stderr.index("-- sync -f /opt/hubinet-ops/.venv.rollback-")
        # rindex, not index: the build tool is also `pct push`-ed during
        # staging (well before activation), so the FIRST occurrence of
        # this substring is that push, not the actual execution below.
        build_at = stderr.rindex("hubinet-ops-update-venv-stage.py")
        assert preserve_at < barrier_at < build_at

    def test_requirements_barrier_crosses_between_preservation_and_activation(self, tmp_path):
        env = seed_installed_environment(tmp_path, installed_requirements="fastapi==0.100.0\n")
        target = build_update_target_checkout(
            tmp_path / "target-barrier-reqs-order", REPO_ROOT, requirements_text="fastapi==0.116.1\n"
        )
        result = _run(env.env, _base_args(target))
        assert result.returncode == 0, result.stderr
        stderr = result.stderr
        preserve_at = stderr.index(
            "-- mv /opt/hubinet-ops/requirements.txt /opt/hubinet-ops/requirements.txt.rollback-"
        )
        barrier_at = stderr.index("-- sync -f /opt/hubinet-ops/requirements.txt.rollback-")
        activate_at = stderr.index("-- mv /opt/hubinet-ops/requirements.txt.staged-")
        assert preserve_at < barrier_at < activate_at

    def test_unit_barrier_crosses_between_finalized_rollback_copy_and_activation(self, tmp_path):
        env = seed_installed_environment(tmp_path)
        target = build_update_target_checkout(
            tmp_path / "target-barrier-unit-order", REPO_ROOT, unit_text="[Unit]\nDescription=changed\n"
        )
        result = _run(env.env, _base_args(target))
        assert result.returncode == 0, result.stderr
        stderr = result.stderr
        finalize_at = stderr.index("-- mv /etc/systemd/system/hubinet-ops.service.rollback-tmp-")
        barrier_at = stderr.index("-- sync -f /etc/systemd/system/hubinet-ops.service.rollback-")
        activate_at = stderr.index("-- mv /etc/systemd/system/hubinet-ops.service.staged-")
        assert finalize_at < barrier_at < activate_at

    def test_marker_barrier_crosses_between_preservation_and_activation(self, tmp_path):
        env = seed_installed_environment(tmp_path, installed_source_sha="8" * 40)
        target = build_update_target_checkout(tmp_path / "target-barrier-marker-order", REPO_ROOT)
        result = _run(env.env, _base_args(target))
        assert result.returncode == 0, result.stderr
        stderr = result.stderr
        preserve_at = stderr.index(
            "-- mv /opt/hubinet-ops/.hubinet-source-commit /opt/hubinet-ops/.hubinet-source-commit.rollback-"
        )
        barrier_at = stderr.index("-- sync -f /opt/hubinet-ops/.hubinet-source-commit.rollback-")
        activate_at = stderr.index("-- mv /opt/hubinet-ops/.hubinet-source-commit.staged-")
        assert preserve_at < barrier_at < activate_at

    def test_final_target_barrier_crosses_between_marker_write_and_boot_reenable(self, tmp_path):
        env = seed_installed_environment(tmp_path, installed_source_sha="9" * 40)
        target = build_update_target_checkout(
            tmp_path / "target-barrier-final-order", REPO_ROOT, unit_text="[Unit]\nDescription=changed\n"
        )
        result = _run(env.env, _base_args(target))
        assert result.returncode == 0, result.stderr
        stderr = result.stderr
        marker_at = stderr.rindex("-- mv /opt/hubinet-ops/.hubinet-source-commit.staged-")
        app_barrier_at = stderr.rindex("-- sync -f /opt/hubinet-ops")
        enable_at = stderr.index("systemctl enable hubinet-ops")
        # Correction pass 10, P1: /etc/systemd/system is now barriered
        # THREE times in this scenario -- the disable-side guard (well
        # before marker_at), this unit-CONTENT barrier (because the unit
        # text changed, between the marker write and re-enable), and the
        # NEW autostart-enablement barrier (after enable_at, covered by
        # TestAutostartEnablementDurabilityBarrier below). `.index(...,
        # marker_at)` finds the first occurrence at/after marker_at,
        # isolating this content barrier specifically.
        unit_content_barrier_at = stderr.index("-- sync -f /etc/systemd/system", marker_at)
        assert marker_at < app_barrier_at < enable_at
        assert marker_at < unit_content_barrier_at < enable_at


class TestForwardDurabilityBarrierFailureSeams:
    def test_app_barrier_failure_rolls_back(self, tmp_path):
        env = seed_installed_environment(
            tmp_path,
            installed_source_sha="1" * 40,
            scenario_overrides={"fail": ["ct_sync_app_rollback"]},
        )
        target = build_update_target_checkout(tmp_path / "target-barrier-app-fail", REPO_ROOT)
        result = _run(env.env, _base_args(target))
        assert result.returncode != 0
        assert "rollback complete" in result.stderr
        assert env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app/__init__.py").exists()
        assert not env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app/inventory/store.py").exists()
        assert env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/.hubinet-source-commit").strip() == "1" * 40
        assert env.state()["vmids"][FAKE_VMID]["service"] == "active"

    def test_venv_barrier_failure_rolls_back(self, tmp_path):
        env = seed_installed_environment(
            tmp_path,
            installed_requirements="fastapi==0.100.0\n",
            scenario_overrides={"fail": ["ct_sync_venv_rollback"]},
        )
        target = build_update_target_checkout(
            tmp_path / "target-barrier-venv-fail", REPO_ROOT, requirements_text="fastapi==0.116.1\n"
        )
        result = _run(env.env, _base_args(target))
        assert result.returncode != 0
        assert "rollback complete" in result.stderr
        assert env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/requirements.txt") == "fastapi==0.100.0\n"
        assert env.ct_file(FAKE_VMID, "/opt/hubinet-ops/.venv/bin/pip").exists()

    def test_requirements_barrier_failure_rolls_back(self, tmp_path):
        env = seed_installed_environment(
            tmp_path,
            installed_requirements="fastapi==0.100.0\n",
            scenario_overrides={"fail": ["ct_sync_requirements_rollback"]},
        )
        target = build_update_target_checkout(
            tmp_path / "target-barrier-reqs-fail", REPO_ROOT, requirements_text="fastapi==0.116.1\n"
        )
        result = _run(env.env, _base_args(target))
        assert result.returncode != 0
        assert "rollback complete" in result.stderr
        assert env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/requirements.txt") == "fastapi==0.100.0\n"

    def test_unit_barrier_failure_rolls_back(self, tmp_path):
        env = seed_installed_environment(tmp_path, scenario_overrides={"fail": ["ct_sync_unit_rollback"]})
        target = build_update_target_checkout(
            tmp_path / "target-barrier-unit-fail", REPO_ROOT, unit_text="[Unit]\nDescription=changed\n"
        )
        result = _run(env.env, _base_args(target))
        assert result.returncode != 0
        assert "rollback complete" in result.stderr
        assert "changed" not in env.ct_file_text(FAKE_VMID, "/etc/systemd/system/hubinet-ops.service")

    def test_marker_barrier_failure_rolls_back(self, tmp_path):
        env = seed_installed_environment(
            tmp_path,
            installed_source_sha="2" * 40,
            scenario_overrides={"fail": ["ct_sync_marker_rollback"]},
        )
        target = build_update_target_checkout(tmp_path / "target-barrier-marker-fail", REPO_ROOT)
        result = _run(env.env, _base_args(target))
        assert result.returncode != 0
        assert "rollback complete" in result.stderr
        assert env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/.hubinet-source-commit").strip() == "2" * 40

    def test_host_helper_forward_barrier_failure_rolls_back(self, tmp_path):
        env = seed_installed_environment(tmp_path)
        env_with_fault = dict(env.env, HUBINET_OPS_TEST_FAIL_HOST_SYNC="rollback-")
        original_helper = env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/app/__init__.py")
        target = build_update_target_checkout(
            tmp_path / "target-barrier-helper-fail",
            REPO_ROOT,
            helper_text="#!/usr/bin/env python3\n# changed helper\n",
        )
        result = _run(env_with_fault, _base_args(target))
        assert result.returncode != 0
        assert "rollback complete" in result.stderr
        assert "changed helper" not in _helper_host_path(env).read_text(encoding="utf-8")

    def test_ct_sync_preflight_failure_stops_before_any_mutation(self, tmp_path):
        env = seed_installed_environment(tmp_path, scenario_overrides={"fail": ["ct_sync_preflight"]})
        target = build_update_target_checkout(tmp_path / "target-barrier-preflight-fail", REPO_ROOT)
        result = _run(env.env, _base_args(target))
        assert result.returncode != 0
        assert "durability barrier (sync -f) is not usable" in result.stderr
        assert not any("systemctl disable" in line for line in result.stderr.splitlines())
        assert env.state()["vmids"][FAKE_VMID]["service"] == "active"
        assert env.state()["vmids"][FAKE_VMID]["service_enabled"] is True

    def test_final_target_barrier_failure_rolls_back_even_after_full_acceptance(self, tmp_path):
        env = seed_installed_environment(
            tmp_path,
            installed_source_sha="3" * 40,
            scenario_overrides={"fail": ["ct_sync_final_app_dir"]},
        )
        target = build_update_target_checkout(tmp_path / "target-barrier-final-fail", REPO_ROOT)
        result = _run(env.env, _base_args(target))
        assert result.returncode != 0
        assert "rollback complete" in result.stderr
        # Acceptance genuinely passed and the marker was genuinely written
        # before the barrier failed -- rollback must still undo all of it.
        assert env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/.hubinet-source-commit").strip() == "3" * 40
        assert not env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app/inventory/store.py").exists()
        assert env.state()["vmids"][FAKE_VMID]["service"] == "active"
        assert env.state()["vmids"][FAKE_VMID]["service_enabled"] is True

    def test_authority_restore_barrier_failure_hard_stops(self, tmp_path):
        env = seed_installed_environment(
            tmp_path,
            schema_version=9,
            scenario_overrides={
                "discovery_result": "backend_unreachable",
                "fail": ["ct_sync_authority_restore"],
            },
        )
        target = build_update_target_checkout(
            tmp_path / "target-barrier-authority-restore-fail", REPO_ROOT, schema_version=10
        )
        result = _run(env.env, _base_args(target, extra=["--allow-authority-reset"]))
        assert result.returncode != 0
        assert "ROLLBACK COULD NOT BE COMPLETED" in result.stderr
        assert "restoring the pre-update authority database" in result.stderr
        assert "rollback complete" not in result.stderr
        # The namespace-level restore already happened -- only the
        # durability proof failed.
        restored = json.loads(env.ct_file_text(FAKE_VMID, "/var/lib/hubinet-ops/authority.db"))
        assert restored["schema_version"] == 9
        assert env.state()["vmids"][FAKE_VMID]["service"] == "inactive"


# ---------------------------------------------------------------------------
# AB. Correction pass 9, P1 -- rollback restoration must also cross its own
#     durability barrier, including on a REPLAY that finds an artifact
#     already restored (section 6).
# ---------------------------------------------------------------------------


class TestRollbackRestorationDurabilityBarrier:
    def test_app_restore_barrier_failure_hard_stops(self, tmp_path):
        env = seed_installed_environment(
            tmp_path,
            installed_source_sha="4" * 40,
            scenario_overrides={"fail": ["mv_staged_app_to_live", "ct_sync_app_restore"]},
        )
        target = build_update_target_checkout(tmp_path / "target-barrier-app-restore-fail", REPO_ROOT)
        result = _run(env.env, _base_args(target))
        assert result.returncode != 0
        assert "ROLLBACK COULD NOT BE COMPLETED" in result.stderr
        assert "restoring the pre-update application payload" in result.stderr
        assert "rollback complete" not in result.stderr
        assert env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app/__init__.py").exists()
        assert not env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app/inventory/store.py").exists()
        assert env.state()["vmids"][FAKE_VMID]["service"] == "inactive"

    def test_replay_of_already_restored_unit_re_establishes_barrier_before_daemon_reload(self, tmp_path):
        original_unit = (REPO_ROOT / "deploy" / "hubinet-ops-0.5.service").read_text(encoding="utf-8")
        env = seed_installed_environment(
            tmp_path,
            scenario_overrides={
                "discovery_result": "backend_unreachable",
                "kill_updater_after_move": "mv_rollback_unit_to_live",
            },
        )
        target = build_update_target_checkout(
            tmp_path / "target-barrier-unit-replay",
            REPO_ROOT,
            unit_text="[Unit]\nDescription=changed target unit\n",
        )
        interrupted = _run(env.env, _base_args(target))
        assert interrupted.returncode == -9
        assert env.ct_file_text(FAKE_VMID, "/etc/systemd/system/hubinet-ops.service") == original_unit

        scenario = json.loads(env.scenario_path.read_text(encoding="utf-8"))
        scenario.pop("kill_updater_after_move", None)
        scenario["fail"] = ["ct_sync_unit_restore"]
        env.scenario_path.write_text(json.dumps(scenario), encoding="utf-8")

        blocked = _run(env.env, _base_args(target))
        assert blocked.returncode != 0
        assert "ROLLBACK COULD NOT BE COMPLETED" in blocked.stderr
        assert "replaying the already-restored systemd unit" in blocked.stderr
        journal = _update_state_path(env, FAKE_VMID, "journal")
        assert journal.exists()
        # Recovery never reached its own daemon-reload -- the barrier
        # failed first.
        assert env.state()["daemon_reload_calls"] == 1


# ---------------------------------------------------------------------------
# AD. Correction pass 10, P1 -- the temporary autostart disable/enable
#     unit-file state is itself ordinary filesystem state under
#     /etc/systemd/system, and must cross the SAME CT durability barrier as
#     every other rollback-critical artifact, on all three sides: the
#     disable side (before the service is stopped or anything else is
#     mutated), the successful re-enable side (before the journal records
#     the run completed), and the rollback/recovery re-enable side (before
#     the old service is started again).
# ---------------------------------------------------------------------------


class TestAutostartEnablementDurabilityBarrier:
    def test_disable_barrier_crosses_before_stop_and_any_mutation(self, tmp_path):
        env = seed_installed_environment(tmp_path, installed_source_sha="a" * 40)
        target = build_update_target_checkout(tmp_path / "target-autostart-disable-order", REPO_ROOT)
        result = _run(env.env, _base_args(target))
        assert result.returncode == 0, result.stderr
        stderr = result.stderr
        disable_at = stderr.index("systemctl disable hubinet-ops")
        barrier_at = stderr.index("-- sync -f /etc/systemd/system")
        stop_at = stderr.index("systemctl stop hubinet-ops")
        assert disable_at < barrier_at < stop_at

    def test_disable_barrier_failure_stops_before_service_stop_and_rolls_back_durably(self, tmp_path):
        # fail_nth_unit_dir_sync=1: the FIRST call to `sync -f
        # /etc/systemd/system` in this run is always the disable-side
        # barrier (it is the very first mutation of the whole window), so
        # this targets it precisely without also tripping the later
        # success/rollback-side barriers that share the same literal path.
        env = seed_installed_environment(
            tmp_path,
            installed_source_sha="b" * 40,
            scenario_overrides={"fail_nth_unit_dir_sync": 1},
        )
        target = build_update_target_checkout(tmp_path / "target-autostart-disable-fail", REPO_ROOT)
        result = _run(env.env, _base_args(target))
        assert result.returncode != 0
        assert "durability barrier failed for /etc/systemd/system" in result.stderr
        # No service stop and no live artifact mutation past the barrier.
        assert not any("systemctl stop" in line for line in result.stderr.splitlines())
        assert "rollback complete" in result.stderr
        assert not env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app/inventory/store.py").exists()
        assert env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/.hubinet-source-commit").strip() == "b" * 40
        assert not _update_state_path(env, FAKE_VMID, "journal").exists()
        post = env.state()["vmids"][FAKE_VMID]
        assert post["service_enabled"] is True
        assert post["service"] == "active"

        # And the restored enablement genuinely IS durable -- not merely
        # namespace-visible in the running kernel.
        env.simulate_pve_ct_reboot(FAKE_VMID)
        assert env.state()["vmids"][FAKE_VMID]["service"] == "active"

    def test_success_enable_barrier_crosses_after_enable_before_reboot_durability(self, tmp_path):
        env = seed_installed_environment(tmp_path, installed_source_sha="c" * 40)
        target = build_update_target_checkout(tmp_path / "target-autostart-enable-order", REPO_ROOT)
        result = _run(env.env, _base_args(target))
        assert result.returncode == 0, result.stderr
        stderr = result.stderr
        enable_at = stderr.index("systemctl enable hubinet-ops")
        # The FIRST /etc/systemd/system barrier at/after enable_at is this
        # new success-side barrier (the disable-side one, much earlier in
        # the run, is strictly before enable_at).
        barrier_at = stderr.index("-- sync -f /etc/systemd/system", enable_at)
        assert enable_at < barrier_at
        assert "in-place update: PASS" in result.stdout
        assert not _update_state_path(env, FAKE_VMID, "journal").exists()

        # The one remaining narrow crash window this barrier closes: a
        # reboot after a fully accepted, durably-enabled target.
        env.simulate_pve_ct_reboot(FAKE_VMID)
        assert env.state()["vmids"][FAKE_VMID]["service"] == "active"

    def test_rollback_enable_barrier_succeeds_and_durably_restores_boot_activation(self, tmp_path):
        # Unlike the forward-path barriers, rollback-restoration barriers
        # (_update_durability_barrier_ct_or_hard_stop) are deliberately not
        # run_logged (see update-recovery.sh's own header comment on that
        # helper family), so there is no "-- sync -f ..." log line to
        # order against here. The failure-injection test below already
        # proves the barrier gates the old service's restart (a failed
        # barrier means the old service is never restarted); this test
        # proves the SUCCESS side of that same ordering end-to-end: after
        # a late-failure rollback, the restored enablement is genuinely
        # DURABLE, not merely the in-kernel view.
        env = seed_installed_environment(
            tmp_path,
            installed_source_sha="d" * 40,
            scenario_overrides={"discovery_result": "backend_unreachable"},
        )
        target = build_update_target_checkout(tmp_path / "target-autostart-rollback-order", REPO_ROOT)
        result = _run(env.env, _base_args(target))
        assert result.returncode != 0
        assert "rollback complete" in result.stderr
        post = env.state()["vmids"][FAKE_VMID]
        assert post["service_enabled"] is True
        assert post["service"] == "active"

        env.simulate_pve_ct_reboot(FAKE_VMID)
        assert env.state()["vmids"][FAKE_VMID]["service"] == "active"

    def test_rollback_enable_barrier_failure_hard_stops_without_false_recovery(self, tmp_path):
        env = seed_installed_environment(
            tmp_path,
            installed_source_sha="e" * 40,
            scenario_overrides={
                "discovery_result": "backend_unreachable",
                # Call #1 is the disable-side barrier (succeeds); call #2
                # is this rollback's own re-enable barrier, since this
                # scenario's unit text never changes (no content-final
                # barrier call in between).
                "fail_nth_unit_dir_sync": 2,
            },
        )
        target = build_update_target_checkout(tmp_path / "target-autostart-rollback-fail", REPO_ROOT)
        result = _run(env.env, _base_args(target))
        assert result.returncode != 0
        assert "ROLLBACK COULD NOT BE COMPLETED" in result.stderr
        assert "restoring hubinet-ops boot activation" in result.stderr
        assert "rollback complete" not in result.stderr
        journal = _update_state_path(env, FAKE_VMID, "journal")
        assert journal.exists()
        # The old service is never (re)started after a failed re-enable
        # barrier -- only the earlier TARGET start (before acceptance
        # failed) appears.
        assert result.stderr.count("systemctl start hubinet-ops") == 1
        # `systemctl enable` itself succeeded in the running kernel, but
        # its own durability barrier failed -- this is exactly the gap
        # this fix closes: the in-kernel view is misleadingly "enabled"
        # while durability is not yet proven.
        assert env.state()["vmids"][FAKE_VMID]["service_enabled"] is True
        env.simulate_pve_ct_reboot(FAKE_VMID)
        assert env.state()["vmids"][FAKE_VMID]["service"] == "inactive", (
            "the enable request succeeded in the running kernel, but its own "
            "durability barrier failed -- a reboot here must not resurrect it"
        )


# ---------------------------------------------------------------------------
# AE. Correction pass 10, P1 -- the newly-created authority backup run
#     directory's ancestry link into its own parent(s) is not proven durable
#     by the authority-tool helper's own file+immediate-directory fsync
#     alone; the caller crosses one more explicit CT filesystem barrier over
#     the backup's run directory before ever treating it as destructively
#     usable.
# ---------------------------------------------------------------------------


class TestAuthorityBackupAncestryDurabilityBarrier:
    def test_backup_dir_barrier_crosses_after_creation_before_boot_reenable(self, tmp_path):
        new_backend_instance_id = "33333333-3333-4333-8333-333333333333"
        env = seed_installed_environment(
            tmp_path, schema_version=9,
            scenario_overrides={"discovery_backend_instance_id": new_backend_instance_id},
        )
        target = build_update_target_checkout(tmp_path / "target-backup-ancestry-order", REPO_ROOT, schema_version=10)
        result = _run(env.env, _base_args(target, extra=["--allow-authority-reset"]))
        assert result.returncode == 0, result.stderr
        stderr = result.stderr
        install_at = stderr.index("install -d -o hubinetops -g hubinetops -m 0750")
        barrier_at = stderr.index("-- sync -f /var/lib/hubinet-ops/update-backups/")
        enable_at = stderr.index("systemctl enable hubinet-ops")
        assert install_at < barrier_at < enable_at

    def test_backup_dir_barrier_failure_blocks_reset_and_preserves_live_db(self, tmp_path):
        new_backend_instance_id = "44444444-4444-4444-8444-444444444444"
        env = seed_installed_environment(
            tmp_path,
            schema_version=9,
            scenario_overrides={
                "discovery_backend_instance_id": new_backend_instance_id,
                "fail": ["ct_sync_authority_backup_dir"],
            },
        )
        target = build_update_target_checkout(tmp_path / "target-backup-ancestry-fail", REPO_ROOT, schema_version=10)
        result = _run(env.env, _base_args(target, extra=["--allow-authority-reset"]))
        assert result.returncode != 0
        assert "rollback complete" in result.stderr

        # The old live database was NEVER removed: the reset-attempted
        # marker (which gates the destructive `remove`) is only journaled
        # AFTER this barrier passes.
        db = json.loads(env.ct_file_text(FAKE_VMID, "/var/lib/hubinet-ops/authority.db"))
        assert db["schema_version"] == 9
        assert db["backend_instance_id"] == FAKE_BACKEND_INSTANCE_ID
        # The coherent backup file itself was created (namespace-visible)
        # before the barrier failed -- retained for manual diagnosis, but
        # this run never treated it as destructively usable.
        backups_root = env.ct_file(FAKE_VMID, "/var/lib/hubinet-ops/update-backups")
        assert list(backups_root.rglob("authority.db"))
        assert not _update_state_path(env, FAKE_VMID, "journal").exists()
        assert env.state()["vmids"][FAKE_VMID]["service"] == "active"
        assert not env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app/inventory/store.py").exists()


# ---------------------------------------------------------------------------
# AC. Correction pass 9, P1 -- immediately-before-mutation ownership + plan
#     fence.
# ---------------------------------------------------------------------------


class TestImmediatelyBeforeMutationFence:
    def test_ordinary_update_passes_through_the_new_fence_and_succeeds(self, tmp_path):
        env = seed_installed_environment(tmp_path)
        target = build_update_target_checkout(tmp_path / "target-fence-ordinary", REPO_ROOT)
        result = _run(env.env, _base_args(target))
        assert result.returncode == 0, result.stderr
        assert "Immediately-before-mutation ownership fence" in result.stderr

    def test_different_installation_run_id_after_staging_is_rejected_before_mutation(self, tmp_path):
        # Test A: a legitimate PVE operator/tool action (e.g. restoring a
        # different CT as this same VMID) between planning and mutation.
        env = seed_installed_environment(
            tmp_path,
            scenario_overrides={
                "swap_installation_identity_after_pubkey_reads": {
                    "after_read_number": 1,
                    "new_run_id": "b" * 32,
                },
            },
        )
        target = build_update_target_checkout(tmp_path / "target-fence-identity-swap", REPO_ROOT)
        result = _run(env.env, _base_args(target))
        assert result.returncode != 0
        assert "immediately-before-mutation ownership fence failed" in result.stderr
        assert f"now carries installation run-id {'b' * 32}" in result.stderr
        assert not any("systemctl disable" in line for line in result.stderr.splitlines())
        assert not any("systemctl stop" in line for line in result.stderr.splitlines())
        assert env.state()["vmids"][FAKE_VMID]["service"] == "active"
        assert env.state()["vmids"][FAKE_VMID]["service_enabled"] is True
        assert not env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app/inventory/store.py").exists()

    def test_requirements_restore_after_unchanged_classification_is_rejected(self, tmp_path):
        # Exact correction-pass-11 witness: classification says unchanged,
        # the same-installation snapshot restore happens only after the
        # complete plan is displayed, and the original classification bytes
        # (not a later baseline re-read) reject it before mutation.
        env = seed_installed_environment(tmp_path)
        target = build_update_target_checkout(tmp_path / "target-fence-reqs-drift", REPO_ROOT)
        result = _run_with_mutation_after_plan(
            env.env,
            _base_args(target),
            lambda: env.ct_file(FAKE_VMID, "/opt/hubinet-ops/requirements.txt").write_text(
                "fastapi==0.100.0\n", encoding="utf-8"
            ),
        )
        assert result.returncode != 0
        assert "immediately-before-mutation plan fence failed" in result.stderr
        assert "requirements.txt changed since the approved plan" in result.stderr
        assert not any("systemctl disable" in line for line in result.stderr.splitlines())
        assert not any("systemctl stop" in line for line in result.stderr.splitlines())
        assert env.state()["vmids"][FAKE_VMID]["service"] == "active"

    def test_changed_unit_drift_after_plan_is_rejected_before_mutation(self, tmp_path):
        env = seed_installed_environment(tmp_path)
        target = build_update_target_checkout(
            tmp_path / "target-fence-unit-drift",
            REPO_ROOT,
            unit_text="[Unit]\nDescription=approved target unit\n",
        )
        result = _run_with_mutation_after_plan(
            env.env,
            _base_args(target),
            lambda: env.ct_file(FAKE_VMID, "/etc/systemd/system/hubinet-ops.service").write_text(
                "[Unit]\nDescription=restored snapshot unit\n", encoding="utf-8"
            ),
        )
        assert result.returncode != 0
        assert "installed systemd unit changed since the approved plan" in result.stderr
        assert "systemctl disable" not in result.stderr
        assert "systemctl stop" not in result.stderr

    def test_changed_helper_drift_after_plan_is_rejected_before_mutation(self, tmp_path):
        env = seed_installed_environment(tmp_path)
        target = build_update_target_checkout(
            tmp_path / "target-fence-helper-drift",
            REPO_ROOT,
            helper_text="#!/usr/bin/env python3\n# approved target helper\n",
        )
        helper_path = (
            Path(env.env["HUBINET_OPS_TEST_HOST_ROOT"])
            / "usr/local/libexec"
            / f"hubinet-package-scan-helper-{FAKE_RUN_ID}"
        )
        result = _run_with_mutation_after_plan(
            env.env,
            _base_args(target),
            lambda: helper_path.write_text(
                "#!/usr/bin/env python3\n# restored snapshot helper\n", encoding="utf-8"
            ),
        )
        assert result.returncode != 0
        assert "installed PVE host helper changed since the approved plan" in result.stderr
        assert "systemctl disable" not in result.stderr
        assert "systemctl stop" not in result.stderr

    def test_trailing_newline_drift_after_plan_is_rejected_byte_exactly(self, tmp_path):
        env = seed_installed_environment(tmp_path)
        target = build_update_target_checkout(tmp_path / "target-fence-newline-drift", REPO_ROOT)
        requirements_path = env.ct_file(FAKE_VMID, "/opt/hubinet-ops/requirements.txt")
        result = _run_with_mutation_after_plan(
            env.env,
            _base_args(target),
            lambda: requirements_path.write_bytes(requirements_path.read_bytes() + b"\n"),
        )
        assert result.returncode != 0
        assert "requirements.txt changed since the approved plan" in result.stderr
        assert "systemctl disable" not in result.stderr
        assert env.state()["vmids"][FAKE_VMID]["service"] == "active"

    def test_preserve_schema_object_drift_after_plan_is_rejected_before_mutation(self, tmp_path):
        # Exact correction-pass-12 witness: preserve classification sees
        # the complete target-required object set, then only one required
        # index disappears after the complete plan is displayed. Marker,
        # version, backend identity, and installation run-id stay fixed.
        env = seed_installed_environment(tmp_path)
        target = build_update_target_checkout(tmp_path / "target-fence-schema-drift", REPO_ROOT)
        authority_path = env.ct_file(FAKE_VMID, "/var/lib/hubinet-ops/authority.db")

        def remove_required_index():
            authority = json.loads(authority_path.read_text(encoding="utf-8"))
            authority["schema_objects"].remove("one_active_endpoint_per_source")
            authority_path.write_text(json.dumps(authority), encoding="utf-8")

        result = _run_with_mutation_after_plan(
            env.env,
            _base_args(target),
            remove_required_index,
        )
        assert result.returncode != 0
        assert "immediately-before-mutation plan fence failed" in result.stderr
        assert "required schema objects" in result.stderr
        assert "rerun planning/approval" in result.stderr
        assert "systemctl disable" not in result.stderr
        assert "systemctl stop" not in result.stderr
        assert env.state()["vmids"][FAKE_VMID]["service"] == "active"
        assert env.state()["vmids"][FAKE_VMID]["service_enabled"] is True
        assert not env.ct_file(FAKE_VMID, "/opt/hubinet-ops/app/inventory/store.py").exists()

    def test_reset_required_does_not_require_old_schema_objects_to_match_target(self, tmp_path):
        env = seed_installed_environment(
            tmp_path,
            schema_version=9,
            schema_objects=["legacy_authority_table"],
            scenario_overrides={
                "discovery_backend_instance_id": "22222222-2222-4222-8222-222222222222",
            },
        )
        target = build_update_target_checkout(
            tmp_path / "target-fence-reset-schema-control", REPO_ROOT, schema_version=10
        )
        result = _run(
            env.env,
            _base_args(target, extra=["--allow-authority-reset"]),
        )
        assert result.returncode == 0, result.stderr
        assert "plan fence failed" not in result.stderr
        assert "RESET" in result.stdout

    def test_discovery_sequence_advancing_normally_does_not_reject(self, tmp_path):
        # Test C: an ordinary background discovery cycle between planning
        # and mutation must never invalidate the update.
        env = seed_installed_environment(
            tmp_path,
            scenario_overrides={"update_probe_sequence_increments_each_call": True},
        )
        target = build_update_target_checkout(tmp_path / "target-fence-sequence-ok", REPO_ROOT)
        result = _run(env.env, _base_args(target))
        assert result.returncode == 0, result.stderr
        assert "plan fence failed" not in result.stderr
        assert "ownership fence failed" not in result.stderr


# ---------------------------------------------------------------------------
# AF. PR #65 correction pass 13, P1 -- the terminal `completed`/`recovered`
#     journal checkpoint must never be reinterpreted as permission to roll
#     back, even though the rollback boundary was crossed earlier in the
#     same run and rollback material may already be partially or fully
#     destroyed by the time a later TERM or cleanup failure reaches the
#     EXIT trap.
# ---------------------------------------------------------------------------


class TestTerminalCheckpointNeverRollsBack:
    def test_term_immediately_after_completed_checkpoint_is_never_rolled_back(self, tmp_path):
        env = seed_installed_environment(tmp_path, installed_source_sha="1" * 40)
        env_with_term = dict(env.env, HUBINET_OPS_TEST_TERM_AT="after_completed_checkpoint")
        target = build_update_target_checkout(tmp_path / "target-term-completed", REPO_ROOT)
        result = _run(env_with_term, _base_args(target))

        assert result.returncode == 143
        assert "rollback complete" not in result.stderr
        assert "ROLLBACK COULD NOT BE COMPLETED" not in result.stderr
        assert "terminal and is never rolled back" in result.stderr
        # No rollback artifact was removed yet, and the target is exactly
        # what this run activated -- never undone.
        assert (
            "Fake target store.py"
            in env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/app/inventory/store.py")
        )
        post = env.state()["vmids"][FAKE_VMID]
        assert post["service"] == "active"
        assert post["service_enabled"] is True
        # A surviving `completed` journal is legitimate here -- cleanup-
        # only, resolved by the next invocation's existing startup
        # recovery.
        journal = _update_state_path(env, FAKE_VMID, "journal")
        if journal.exists():
            assert "state=completed" in journal.read_text(encoding="utf-8")

    def test_term_after_partial_completed_cleanup_is_never_rolled_back(self, tmp_path):
        env = seed_installed_environment(tmp_path, installed_source_sha="2" * 40)
        env_with_term = dict(env.env, HUBINET_OPS_TEST_TERM_AT="after_completed_partial_cleanup")
        target = build_update_target_checkout(tmp_path / "target-term-partial-cleanup", REPO_ROOT)
        result = _run(env_with_term, _base_args(target))

        assert result.returncode == 143
        assert "rollback complete" not in result.stderr
        assert "ROLLBACK COULD NOT BE COMPLETED" not in result.stderr
        # Some rollback artifacts are already destroyed -- rollback must
        # never attempt to restore old app/venv/unit/db from here.
        assert not list(env.ct_file(FAKE_VMID, "/opt/hubinet-ops").glob("app.rollback-*"))
        assert (
            "Fake target store.py"
            in env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/app/inventory/store.py")
        )
        post = env.state()["vmids"][FAKE_VMID]
        assert post["service"] == "active"
        assert post["service_enabled"] is True

    def test_authority_reset_completed_witness_never_restores_old_db(self, tmp_path):
        # The destructive schema-reset witness: after a completed run whose
        # authority action was reset_required, a TERM landing after the
        # completed checkpoint (with rollback material already partly
        # destroyed) must never see the old validated backup copied back
        # over the TARGET database.
        new_backend_instance_id = "44444444-4444-4444-8444-444444444444"
        env = seed_installed_environment(
            tmp_path,
            schema_version=9,
            scenario_overrides={"discovery_backend_instance_id": new_backend_instance_id},
        )
        env_with_term = dict(env.env, HUBINET_OPS_TEST_TERM_AT="after_completed_partial_cleanup")
        target = build_update_target_checkout(
            tmp_path / "target-term-schema-reset", REPO_ROOT, schema_version=10
        )
        result = _run(env_with_term, _base_args(target, extra=["--allow-authority-reset"]))

        assert result.returncode == 143
        assert "rollback complete" not in result.stderr
        assert "ROLLBACK COULD NOT BE COMPLETED" not in result.stderr
        # A completed reset removes the old live database and never
        # recreates it itself (the target runtime does that on its own
        # next start -- see TestAuthorityReset's own positive control).
        # The witness: a TERM after the completed checkpoint must NEVER
        # see the retained OLD backup copied back over this absent path.
        db_path = env.ct_file(FAKE_VMID, "/var/lib/hubinet-ops/authority.db")
        assert not db_path.exists()
        backups_root = env.ct_file(FAKE_VMID, "/var/lib/hubinet-ops/update-backups")
        backup_files = list(backups_root.rglob("authority.db"))
        assert backup_files, "expected the retained authority DB backup"
        backup_data = json.loads(backup_files[0].read_text(encoding="utf-8"))
        assert backup_data["schema_version"] == 9
        assert backup_data["backend_instance_id"] == FAKE_BACKEND_INSTANCE_ID

    def test_journal_clear_failure_after_completed_is_resolved_by_next_invocation(self, tmp_path):
        env = seed_installed_environment(tmp_path, installed_source_sha="3" * 40)
        env_with_fault = dict(env.env, HUBINET_OPS_TEST_FAIL_JOURNAL_CLEAR="1")
        target = build_update_target_checkout(tmp_path / "target-journal-clear-fail", REPO_ROOT)

        first = _run(env_with_fault, _base_args(target))
        assert first.returncode != 0
        assert "rollback complete" not in first.stderr
        assert "ROLLBACK COULD NOT BE COMPLETED" not in first.stderr
        journal = _update_state_path(env, FAKE_VMID, "journal")
        assert journal.exists()
        assert "state=completed" in journal.read_text(encoding="utf-8")
        assert (
            "Fake target store.py"
            in env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/app/inventory/store.py")
        )

        # The next invocation's existing startup-recovery `completed` path
        # resolves it -- proving enabled+active+healthy first -- never by
        # rolling back.
        second = _run(env.env, _base_args(target))
        assert second.returncode == 0, second.stderr
        assert "was already" in second.stderr
        assert not journal.exists()

    def test_active_before_completed_still_rolls_back(self, tmp_path):
        # Negative control: this fix must not weaken ordinary ACTIVE-state
        # rollback -- a failure strictly BEFORE the completed checkpoint
        # still rolls back exactly as before.
        env = seed_installed_environment(
            tmp_path, scenario_overrides={"discovery_result": "backend_unreachable"}
        )
        target = build_update_target_checkout(tmp_path / "target-active-still-rolls-back", REPO_ROOT)
        result = _run(env.env, _base_args(target))
        assert result.returncode != 0
        assert "rollback complete" in result.stderr


# ---------------------------------------------------------------------------
# Correction pass 15, P2 -- the `completed` journal must survive until
# every run-owned cleanup operation this run's own terminal cleanup
# performs has actually succeeded (_update_cleanup_recovered_run_
# artifacts, now reused by _update_finish_summary instead of a second,
# divergent "|| true" cleanup). A cleanup failure here is NEVER silently
# swallowed: it hard-stops with the completed journal still durable, and
# the next invocation's existing startup-recovery `completed` path
# retries the exact same cleanup and only then clears the journal.
# ---------------------------------------------------------------------------


class TestCompletedCleanupStrictBeforeJournalClear:
    def test_ct_cleanup_failure_retains_journal_and_replay_finishes_it(self, tmp_path):
        env = seed_installed_environment(tmp_path, installed_source_sha="7" * 40)
        env_with_fault = dict(env.env, HUBINET_OPS_TEST_FAIL_CT_CLEANUP="1")
        target = build_update_target_checkout(tmp_path / "target-ct-cleanup-fail", REPO_ROOT)

        first = _run(env_with_fault, _base_args(target))
        assert first.returncode != 0
        assert "rollback complete" not in first.stderr
        assert "ROLLBACK COULD NOT BE COMPLETED" in first.stderr
        journal = _update_state_path(env, FAKE_VMID, "journal")
        assert journal.exists()
        assert "state=completed" in journal.read_text(encoding="utf-8")
        # The target is fully accepted and live, and the leftover rollback
        # artifact this run's cleanup could not remove is still there,
        # proving cleanup really failed rather than being skipped.
        assert (
            "Fake target store.py"
            in env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/app/inventory/store.py")
        )
        assert list(env.ct_file(FAKE_VMID, "/opt/hubinet-ops").glob("app.rollback-*"))
        post = env.state()["vmids"][FAKE_VMID]
        assert post["service"] == "active"
        assert post["service_enabled"] is True

        # A later invocation (fault cleared) resolves the surviving
        # `completed` journal through the existing startup-recovery path:
        # re-proves enabled+active+healthy, retries the SAME strict
        # cleanup, removes the leftover, and only then clears the journal.
        second = _run(env.env, _base_args(target))
        assert second.returncode == 0, second.stderr
        assert "was already" in second.stderr
        assert not journal.exists()
        assert not list(env.ct_file(FAKE_VMID, "/opt/hubinet-ops").glob("app.rollback-*"))

    def test_host_helper_cleanup_failure_retains_journal_and_replay_finishes_it(self, tmp_path):
        env = seed_installed_environment(
            tmp_path, installed_source_sha="8" * 40, installed_helper_text=OLD_HELPER_TEXT
        )
        env_with_fault = dict(env.env, HUBINET_OPS_TEST_FAIL_HOST_CLEANUP="1")
        target = build_update_target_checkout(
            tmp_path / "target-host-cleanup-fail", REPO_ROOT, helper_text=NEW_HELPER_TEXT
        )

        first = _run(env_with_fault, _base_args(target))
        assert first.returncode != 0
        assert "ROLLBACK COULD NOT BE COMPLETED" in first.stderr
        journal = _update_state_path(env, FAKE_VMID, "journal")
        assert journal.exists()
        assert "state=completed" in journal.read_text(encoding="utf-8")
        helper_live = _helper_host_path(env)
        assert helper_live.read_text(encoding="utf-8") == NEW_HELPER_TEXT
        # The CT-side steps (this fault is the LAST of the three cleanup
        # steps) already fully succeeded -- only the host-side leftover
        # survives.
        assert not list(env.ct_file(FAKE_VMID, "/opt/hubinet-ops").glob("app.rollback-*"))
        assert list(helper_live.parent.glob(f"{helper_live.name}.rollback-*"))

        second = _run(env.env, _base_args(target))
        assert second.returncode == 0, second.stderr
        assert "was already" in second.stderr
        assert not journal.exists()
        assert not list(helper_live.parent.glob(f"{helper_live.name}.rollback-*"))

    def test_partial_cleanup_then_later_step_fails_replay_finishes_remaining(self, tmp_path):
        # Some cleanup already succeeded (CT rollback/staged artifacts and
        # planning tools), a LATER step fails (the host-side helper) -- the
        # journal is retained, and a replay idempotently finishes only
        # what remains rather than re-doing (or falsely re-failing on)
        # what is already gone.
        env = seed_installed_environment(
            tmp_path, installed_source_sha="9" * 40, installed_helper_text=OLD_HELPER_TEXT
        )
        env_with_fault = dict(env.env, HUBINET_OPS_TEST_FAIL_HOST_CLEANUP="1")
        target = build_update_target_checkout(
            tmp_path / "target-partial-cleanup", REPO_ROOT, helper_text=NEW_HELPER_TEXT
        )

        first = _run(env_with_fault, _base_args(target))
        assert first.returncode != 0
        assert not list(env.ct_file(FAKE_VMID, "/opt/hubinet-ops").glob("app.rollback-*"))
        assert not list(env.ct_file(FAKE_VMID, "/opt/hubinet-ops").glob(".venv.rollback-*"))
        helper_live = _helper_host_path(env)
        assert list(helper_live.parent.glob(f"{helper_live.name}.rollback-*"))
        journal = _update_state_path(env, FAKE_VMID, "journal")
        assert "state=completed" in journal.read_text(encoding="utf-8")

        second = _run(env.env, _base_args(target))
        assert second.returncode == 0, second.stderr
        assert not journal.exists()
        assert not list(helper_live.parent.glob(f"{helper_live.name}.rollback-*"))

    def test_ordinary_successful_update_still_clears_journal_immediately(self, tmp_path):
        # Negative control: the strict cleanup contract changes nothing
        # about the ordinary success case -- the journal is gone after a
        # single successful invocation, no replay required.
        env = seed_installed_environment(tmp_path, installed_source_sha="b" * 40)
        target = build_update_target_checkout(tmp_path / "target-ordinary-cleanup", REPO_ROOT)
        result = _run(env.env, _base_args(target))
        assert result.returncode == 0, result.stderr
        assert not _update_state_path(env, FAKE_VMID, "journal").exists()
        assert not list(env.ct_file(FAKE_VMID, "/opt/hubinet-ops").glob("app.rollback-*"))


def _override_scenario(env, **overrides):
    scenario = json.loads(env.scenario_path.read_text(encoding="utf-8"))
    scenario.update(overrides)
    env.scenario_path.write_text(json.dumps(scenario), encoding="utf-8")


def _reset_health_call_counter(env):
    state = env.state()
    state["backend_health_calls"] = 0
    env.state_path.write_text(json.dumps(state), encoding="utf-8")


# ---------------------------------------------------------------------------
# Correction pass 15, P2/finding #3 -- terminal (`completed`/`recovered`)
# startup recovery must use the SAME bounded systemd-active + HTTP-health
# poll as forward activation and rollback, not a one-shot proof
# (_update_prove_service_enabled_active_and_healthy now reuses
# _update_wait_until_service_active_and_healthy after a single positive
# enablement probe). After a genuine PVE/CT reboot, systemd reports the
# Type=simple unit `active` strictly before uvicorn has bound its port --
# exactly the same ordinary readiness race already corrected on the
# forward/rollback paths; a one-shot probe fired at the wrong instant
# would hard-stop resolving an already-accepted `completed` journal.
# ---------------------------------------------------------------------------


class TestTerminalReadinessBoundedReuseP2:
    def test_completed_journal_survives_reboot_with_delayed_health(self, tmp_path):
        env = seed_installed_environment(tmp_path, installed_source_sha="c" * 40)
        env_with_term = dict(env.env, HUBINET_OPS_TEST_TERM_AT="after_completed_checkpoint")
        target = build_update_target_checkout(tmp_path / "target-terminal-reboot-delay", REPO_ROOT)

        interrupted = _run(env_with_term, _base_args(target))
        assert interrupted.returncode == 143
        journal = _update_state_path(env, FAKE_VMID, "journal")
        assert journal.exists()
        assert "state=completed" in journal.read_text(encoding="utf-8")

        env.simulate_pve_ct_reboot(FAKE_VMID)
        rebooted = env.state()["vmids"][FAKE_VMID]
        # systemd reports `active` immediately (Type=simple) -- strictly
        # before uvicorn has necessarily answered its own health endpoint.
        assert rebooted["service"] == "active"
        assert rebooted["service_enabled"] is True

        _reset_health_call_counter(env)
        _override_scenario(env, health_fail_first_n=3)
        env.env["BOOTSTRAP_SERVICE_TIMEOUT_SECONDS"] = "10"

        recovered = _run(env.env, _base_args(target))
        assert recovered.returncode == 0, recovered.stderr
        assert "was already" in recovered.stderr
        assert env.state()["backend_health_calls"] == 4
        assert not journal.exists()

    def test_completed_journal_survives_reboot_with_stalled_health(self, tmp_path):
        # Same race, but through the bounded --max-time contract (finding
        # #1) rather than an ordinary connection-refused retry.
        env = seed_installed_environment(tmp_path, installed_source_sha="1234" * 10)
        env_with_term = dict(env.env, HUBINET_OPS_TEST_TERM_AT="after_completed_checkpoint")
        target = build_update_target_checkout(tmp_path / "target-terminal-reboot-stall", REPO_ROOT)

        interrupted = _run(env_with_term, _base_args(target))
        assert interrupted.returncode == 143

        env.simulate_pve_ct_reboot(FAKE_VMID)
        _reset_health_call_counter(env)
        _override_scenario(env, health_stall_first_n=3)
        env.env["BOOTSTRAP_SERVICE_TIMEOUT_SECONDS"] = "10"

        recovered = _run(env.env, _base_args(target))
        assert recovered.returncode == 0, recovered.stderr
        assert "FAKE-CURL" not in recovered.stderr
        assert env.state()["backend_health_calls"] == 4
        assert not _update_state_path(env, FAKE_VMID, "journal").exists()

    def test_completed_journal_health_never_ready_after_reboot_hard_stops_and_retains_journal(
        self, tmp_path
    ):
        env = seed_installed_environment(tmp_path, installed_source_sha="5678" * 10)
        env_with_term = dict(env.env, HUBINET_OPS_TEST_TERM_AT="after_completed_checkpoint")
        target = build_update_target_checkout(tmp_path / "target-terminal-reboot-dead", REPO_ROOT)

        interrupted = _run(env_with_term, _base_args(target))
        assert interrupted.returncode == 143

        env.simulate_pve_ct_reboot(FAKE_VMID)
        _override_scenario(env, fail=["backend_health"])
        env.env["BOOTSTRAP_SERVICE_TIMEOUT_SECONDS"] = "3"

        result = _run(env.env, _base_args(target))
        assert result.returncode != 0
        assert "ROLLBACK COULD NOT BE COMPLETED" in result.stderr
        assert "does not now prove enabled + active + healthy" in result.stderr
        journal = _update_state_path(env, FAKE_VMID, "journal")
        assert journal.exists()
        assert "state=completed" in journal.read_text(encoding="utf-8")

    def test_completed_journal_systemd_failed_after_reboot_is_terminal_hard_stop(self, tmp_path):
        env = seed_installed_environment(tmp_path, installed_source_sha="9abc" * 10)
        env_with_term = dict(env.env, HUBINET_OPS_TEST_TERM_AT="after_completed_checkpoint")
        target = build_update_target_checkout(tmp_path / "target-terminal-reboot-failed", REPO_ROOT)

        interrupted = _run(env_with_term, _base_args(target))
        assert interrupted.returncode == 143

        env.simulate_pve_ct_reboot(FAKE_VMID)
        _override_scenario(env, service_active_override="failed")

        result = _run(env.env, _base_args(target))
        assert result.returncode != 0
        assert "ROLLBACK COULD NOT BE COMPLETED" in result.stderr
        journal = _update_state_path(env, FAKE_VMID, "journal")
        assert journal.exists()
        assert "state=completed" in journal.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# AG. PR #65 correction pass 13, P2 -- the interrupted-update journal
#     validator must accept every run-id shape the shared
#     bootstrap-common.sh::_generate_run_id can actually produce, including
#     its numeric fallback, while still rejecting anything else.
# ---------------------------------------------------------------------------


def _write_hand_crafted_journal(env, *, run_id, installation_run_id=FAKE_RUN_ID, state="active", rollback_armed="0"):
    path = _update_state_path(env, FAKE_VMID, "journal")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "format=hubinet-ops-update-journal-v1\n"
        f"state={state}\n"
        f"vmid={FAKE_VMID}\n"
        f"run_id={run_id}\n"
        f"installation_run_id={installation_run_id}\n"
        f"rollback_armed={rollback_armed}\n"
        "requirements_changed=0\n"
        "authority_action=\n"
        "db_backup_path=\n",
        encoding="utf-8",
    )
    return path


class TestRunIdValidatorAcceptsGeneratorFormats:
    def test_fallback_shaped_run_id_journal_loads_and_recovery_proceeds(self, tmp_path):
        # state=active, rollback_armed=0: the simplest recovery path
        # (cleanup this run's own staged artifacts, then prove the
        # untouched pre-mutation service). Isolates "does the validator
        # accept this run_id shape at all" from the full rollback machinery.
        env = seed_installed_environment(tmp_path)
        journal = _write_hand_crafted_journal(env, run_id="1700000000123456789-4242-1111122222")
        target = build_update_target_checkout(tmp_path / "target-run-id-fallback-simple", REPO_ROOT)

        result = _run(env.env, _base_args(target))
        assert result.returncode == 0, result.stderr
        assert "was recovered" in result.stderr
        assert "failed validation" not in result.stderr
        assert "has an invalid run id" not in result.stderr
        assert not journal.exists()

    @pytest.mark.parametrize(
        "bad_run_id",
        [
            "../../etc/passwd",
            "run/id",
            "run id",
            "not-hex-but-alphabetic",
            "1-2-",
            "",
            # PR #65 correction pass 14, P2: the normal branch is exactly
            # the 32 lowercase-hex characters _generate_run_id's normal
            # path can produce, not the broader "any length of hex" the
            # old ^[0-9a-f]+$ check accepted.
            "a",
            "a1b2c3d4e5f60718293a4b5c6d7e8f9",  # 31 hex chars
            "a1b2c3d4e5f60718293a4b5c6d7e8f900",  # 33 hex chars
            "A1B2C3D4E5F60718293A4B5C6D7E8F90",  # uppercase 32 hex chars
        ],
    )
    def test_malformed_run_id_variants_are_rejected_and_journal_preserved(self, tmp_path, bad_run_id):
        env = seed_installed_environment(tmp_path)
        journal = _write_hand_crafted_journal(env, run_id=bad_run_id)
        target = build_update_target_checkout(tmp_path / "target-bad-run-id", REPO_ROOT)

        result = _run(env.env, _base_args(target))
        assert result.returncode != 0, bad_run_id
        assert "has an invalid run id" in result.stderr, (bad_run_id, result.stderr)
        assert journal.exists()
        assert not any("systemctl disable" in line for line in result.stderr.splitlines())

    def test_normal_hex_run_id_journal_still_loads(self, tmp_path):
        # Regression control for the existing, unmodified branch of the
        # validator.
        env = seed_installed_environment(tmp_path)
        journal = _write_hand_crafted_journal(env, run_id="a1b2c3d4e5f60718293a4b5c6d7e8f90")
        target = build_update_target_checkout(tmp_path / "target-run-id-hex", REPO_ROOT)

        result = _run(env.env, _base_args(target))
        assert result.returncode == 0, result.stderr
        assert "was recovered" in result.stderr
        assert not journal.exists()

    def test_fallback_shaped_active_rollback_armed_run_recovers_coherently(self, tmp_path):
        # The strongest regression: a REAL fallback-shaped UPDATE_RUN_ID
        # (HUBINET_OPS_TEST_FORCE_RUN_ID_FALLBACK, exercising
        # bootstrap-common.sh::_generate_run_id's own fallback branch),
        # SIGKILLed mid-rollback-armed mutation, recovered on the next
        # invocation exactly like the existing hex-run-id interruption
        # tests -- proving every run-owned path this run's journal
        # reconstructs (authority helper, rollback artifacts) still
        # resolves correctly under the fallback shape.
        env = seed_installed_environment(
            tmp_path,
            scenario_overrides={
                "discovery_result": "backend_unreachable",
                "kill_updater_after_move": "mv_live_app_to_rollback",
            },
        )
        env_forced_fallback = dict(env.env, HUBINET_OPS_TEST_FORCE_RUN_ID_FALLBACK="1")
        target = build_update_target_checkout(tmp_path / "target-run-id-fallback-armed", REPO_ROOT)

        interrupted = _run(env_forced_fallback, _base_args(target))
        assert interrupted.returncode == -9
        journal = _update_state_path(env, FAKE_VMID, "journal")
        assert journal.exists()
        journal_text = journal.read_text(encoding="utf-8")
        run_id = _journal_run_id(journal_text)
        assert re.fullmatch(r"[0-9]+-[0-9]+-[0-9]+", run_id), run_id

        recovered = _run(env.env, _base_args(target))
        assert recovered.returncode == 0, recovered.stderr
        assert "was recovered" in recovered.stderr
        assert "failed validation" not in recovered.stderr
        assert not journal.exists()
        post = env.state()["vmids"][FAKE_VMID]
        assert post["service"] == "active"
        assert post["service_enabled"] is True


# ---------------------------------------------------------------------------
# AH. PR #65 correction pass 13, P2 -- rollback of a systemd unit whose
#     [Install] section changed must reset the unit's boot-activation
#     links to the RESTORED old unit's own declaration (`systemctl
#     reenable`), not merely add the old links on top of whatever the
#     (now-superseded) target unit's own `enable` already installed
#     (`systemctl enable` is additive only).
# ---------------------------------------------------------------------------


class TestRollbackResetsStaleEnablementLinks:
    OLD_UNIT = "[Unit]\nDescription=pre-update unit\n\n[Install]\nWantedBy=multi-user.target\n"
    NEW_UNIT = (
        "[Unit]\nDescription=target unit\n\n"
        "[Install]\nWantedBy=graphical.target\nAlias=hubinet-target.service\n"
    )

    def test_changed_unit_rollback_resets_stale_target_links(self, tmp_path):
        env = seed_installed_environment(
            tmp_path,
            installed_source_sha="f" * 40,
            installed_unit_text=self.OLD_UNIT,
            # Target activation, including its own `systemctl enable`,
            # genuinely succeeds -- only the FINAL unit-enablement
            # durability barrier (the third /etc/systemd/system barrier in
            # a changed-unit run: disable-side, unit-content, final
            # enable) fails, exactly the finding's own scenario.
            scenario_overrides={"fail_nth_unit_dir_sync": 3},
        )
        target = build_update_target_checkout(
            tmp_path / "target-stale-links", REPO_ROOT, unit_text=self.NEW_UNIT
        )
        result = _run(env.env, _base_args(target))

        assert result.returncode != 0
        assert "rollback complete" in result.stderr
        enable_at = result.stderr.index("systemctl enable hubinet-ops")
        reenable_at = result.stderr.index("systemctl reenable hubinet-ops")
        assert enable_at < reenable_at

        assert env.ct_file_text(FAKE_VMID, "/etc/systemd/system/hubinet-ops.service") == self.OLD_UNIT
        links = env.state()["vmids"][FAKE_VMID]["service_enable_links"]
        assert links == ["multi-user.target.wants/hubinet-ops.service"]
        post = env.state()["vmids"][FAKE_VMID]
        assert post["service"] == "active"
        assert post["service_enabled"] is True

        # And the reset is genuinely durable.
        env.simulate_pve_ct_reboot(FAKE_VMID)
        state_after_reboot = env.state()["vmids"][FAKE_VMID]
        assert state_after_reboot["service"] == "active"
        assert state_after_reboot["service_enable_links"] == ["multi-user.target.wants/hubinet-ops.service"]

    def test_reenable_failure_hard_stops_without_false_recovery(self, tmp_path):
        env = seed_installed_environment(
            tmp_path,
            installed_source_sha="e" * 40,
            installed_unit_text=self.OLD_UNIT,
            scenario_overrides={
                "discovery_result": "backend_unreachable",
                "fail": ["service_autostart_reenable"],
            },
        )
        target = build_update_target_checkout(
            tmp_path / "target-stale-links-reenable-fail", REPO_ROOT, unit_text=self.NEW_UNIT
        )
        result = _run(env.env, _base_args(target))

        assert result.returncode != 0
        assert "ROLLBACK COULD NOT BE COMPLETED" in result.stderr
        assert "re-enabled (reenable)" in result.stderr
        assert "rollback complete" not in result.stderr
        journal = _update_state_path(env, FAKE_VMID, "journal")
        assert journal.exists()
        # The old unit FILE is already restored -- only re-enrolling its
        # boot-activation links failed.
        assert env.ct_file_text(FAKE_VMID, "/etc/systemd/system/hubinet-ops.service") == self.OLD_UNIT

    def test_reenable_failure_after_target_already_enabled_hard_stops(self, tmp_path):
        # PR #65 correction pass 14, P2 -- exact missing witness. Unlike
        # test_reenable_failure_hard_stops_without_false_recovery above
        # (which forces rollback via an EARLIER discovery failure, before
        # the target's own `systemctl enable` ever runs, so the unit is
        # still disabled and the UnitFileState probe trivially catches the
        # failed reenable), this enters rollback only AFTER the target's
        # forward `systemctl enable` has already succeeded -- exactly the
        # third /etc/systemd/system durability barrier
        # (fail_nth_unit_dir_sync=3, the same seam
        # test_changed_unit_rollback_resets_stale_target_links uses for its
        # successful case) -- so target-only links are already installed
        # and UnitFileState is already "enabled" GOING IN to the failed
        # rollback reenable. A probe-only proof would (incorrectly) accept
        # that pre-existing state as evidence the reenable's stale-link
        # reset actually happened.
        env = seed_installed_environment(
            tmp_path,
            installed_source_sha="d" * 40,
            installed_unit_text=self.OLD_UNIT,
            scenario_overrides={
                "fail_nth_unit_dir_sync": 3,
                "fail": ["service_autostart_reenable"],
            },
        )
        target = build_update_target_checkout(
            tmp_path / "target-stale-links-already-enabled-reenable-fail",
            REPO_ROOT,
            unit_text=self.NEW_UNIT,
        )
        result = _run(env.env, _base_args(target))

        assert result.returncode != 0
        assert "ROLLBACK COULD NOT BE COMPLETED" in result.stderr
        assert "re-enabled (reenable)" in result.stderr
        assert "rollback complete" not in result.stderr
        journal = _update_state_path(env, FAKE_VMID, "journal")
        assert journal.exists()
        # The old unit FILE is already restored -- only re-enrolling its
        # boot-activation links failed.
        assert env.ct_file_text(FAKE_VMID, "/etc/systemd/system/hubinet-ops.service") == self.OLD_UNIT
        # The pre-existing (target-only) enabled state must never be
        # mistaken for a successfully restored boot activation: the stale
        # target links from forward activation are still exactly what is
        # installed -- the old unit's own links were never re-established
        # -- and the old service is never restarted on a failed proof.
        post = env.state()["vmids"][FAKE_VMID]
        assert post["service_enabled"] is True
        assert post["service_enable_links"] == sorted(
            {"graphical.target.wants/hubinet-ops.service", "hubinet-target.service"}
        )
        assert post["service"] != "active"

    def test_reenable_failure_diagnostic_reports_the_real_distinctive_exit_status(self, tmp_path):
        # PR #65 correction pass 15, P3 -- `if ! run_logged ...; then
        # rc=$?; fi` read `$?` from the NEGATED `!` compound condition
        # (always 0 in that branch), not the underlying `systemctl
        # reenable`'s own exit status. The safety behavior was already
        # correct (fail closed on any nonzero); only the diagnostic was
        # false. A distinctive, non-generic exit code proves the real
        # status is now captured and surfaced.
        env = seed_installed_environment(
            tmp_path,
            installed_source_sha="f" * 40,
            installed_unit_text=self.OLD_UNIT,
            scenario_overrides={
                "discovery_result": "backend_unreachable",
                "fail": ["service_autostart_reenable"],
                "service_autostart_reenable_exit_code": 17,
            },
        )
        target = build_update_target_checkout(
            tmp_path / "target-reenable-distinctive-rc", REPO_ROOT, unit_text=self.NEW_UNIT
        )
        result = _run(env.env, _base_args(target))

        assert result.returncode != 0
        assert "ROLLBACK COULD NOT BE COMPLETED" in result.stderr
        assert "exit 17" in result.stderr
        assert "rollback complete" not in result.stderr

    def test_unchanged_unit_rollback_never_calls_reenable(self, tmp_path):
        env = seed_installed_environment(
            tmp_path,
            installed_source_sha="0" * 40,
            scenario_overrides={"discovery_result": "backend_unreachable"},
        )
        target = build_update_target_checkout(tmp_path / "target-stale-links-unchanged", REPO_ROOT)
        result = _run(env.env, _base_args(target))

        assert result.returncode != 0
        assert "rollback complete" in result.stderr
        assert "systemctl reenable" not in result.stderr
        assert env.state()["vmids"][FAKE_VMID].get("service_autostart_reenable_calls", 0) == 0


# ---------------------------------------------------------------------------
# The five package-update forced-command boundaries.
#
# Two paths matter here and are genuinely different. Updating an ALREADY
# activated installation replaces helper content in place; upgrading a
# PRE-ACTIVATION one CREATES new privileged access paths, and is therefore
# the one whose rollback has to remove a key, an authorized_keys entry, and
# a root-owned mutation helper rather than merely restore a file.
# ---------------------------------------------------------------------------


BOUNDARY_KINDS = tuple(kind for kind, _name in UPDATE_BOUNDARY_HELPERS)


def _host_root(env):
    return Path(env.env["HUBINET_OPS_TEST_HOST_ROOT"])


def _authorized_keys_text(env):
    path = _host_root(env) / "root" / ".ssh" / "authorized_keys"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _boundary_helper(env, kind, run_id=FAKE_RUN_ID):
    return _host_root(env) / boundary_helper_host_path(kind, run_id).lstrip("/")


class TestActivatedInstallationBoundaries:
    def test_an_ordinary_update_leaves_every_boundary_untouched(self, tmp_path):
        """Nothing to do is nothing done: no key rotation, no re-authorization."""

        env = seed_installed_environment(tmp_path, installed_source_sha="1" * 40)
        before = _authorized_keys_text(env)
        before_helpers = {
            kind: _boundary_helper(env, kind).read_text(encoding="utf-8")
            for kind in BOUNDARY_KINDS
        }
        before_keys = {
            kind: env.ct_file_text(FAKE_VMID, boundary_key_ct_path(kind))
            for kind in BOUNDARY_KINDS
        }
        target = build_update_target_checkout(tmp_path / "target", REPO_ROOT)

        result = _run(env.env, _base_args(target))

        assert result.returncode == 0, result.stderr
        assert _authorized_keys_text(env) == before
        for kind in BOUNDARY_KINDS:
            assert _boundary_helper(env, kind).read_text(encoding="utf-8") == (
                before_helpers[kind]
            ), kind
            assert env.ct_file_text(FAKE_VMID, boundary_key_ct_path(kind)) == (
                before_keys[kind]
            ), kind
        assert "unchanged -- all five forced-command boundaries" in result.stdout

    def test_a_changed_boundary_helper_is_replaced_at_the_same_path(self, tmp_path):
        """Content changes; the path, the key, and the authorization do not."""

        env = seed_installed_environment(
            tmp_path,
            installed_source_sha="1" * 40,
            boundary_helper_text={"mutation": "#!/usr/bin/env python3\n# stale\n"},
        )
        before_authorized = _authorized_keys_text(env)
        target = build_update_target_checkout(tmp_path / "target", REPO_ROOT)

        result = _run(env.env, _base_args(target))

        assert result.returncode == 0, result.stderr
        installed = _boundary_helper(env, "mutation").read_text(encoding="utf-8")
        expected = (
            REPO_ROOT / "deploy" / "hubinet-package-mutation-helper.py"
        ).read_text(encoding="utf-8")
        assert installed == expected
        assert _authorized_keys_text(env) == before_authorized
        assert "1 replaced in place" in result.stdout

    def test_a_repeated_update_is_idempotent(self, tmp_path):
        env = seed_installed_environment(tmp_path, installed_source_sha="1" * 40)
        target = build_update_target_checkout(tmp_path / "target", REPO_ROOT)

        first = _run(env.env, _base_args(target))
        assert first.returncode == 0, first.stderr
        after_first = _authorized_keys_text(env)
        second = _run(env.env, _base_args(target))

        assert second.returncode == 0, second.stderr
        assert _authorized_keys_text(env) == after_first
        helper_dir = _host_root(env) / "usr" / "local" / "libexec"
        for kind in BOUNDARY_KINDS:
            assert len(list(helper_dir.glob(f"hubinet-package-{kind}-boundary-*"))) == 1


class TestPreActivationInstallationUpgrade:
    """A pre-activation installation upgraded into the activated lifecycle."""

    def test_the_upgrade_creates_every_boundary_and_activates_the_config(
        self, tmp_path
    ):
        env = seed_installed_environment(
            tmp_path, installed_source_sha="1" * 40, activated=False
        )
        unrelated = "ssh-ed25519 QUFBQUFBQUFBQUFBQUFBQUFBQUFB pve-operator\n"
        authorized_path = _host_root(env) / "root" / ".ssh" / "authorized_keys"
        authorized_path.write_text(
            authorized_path.read_text(encoding="utf-8") + unrelated, encoding="utf-8"
        )
        target = build_update_target_checkout(tmp_path / "target", REPO_ROOT)

        result = _run(env.env, _base_args(target))

        assert result.returncode == 0, result.stderr
        authorized = _authorized_keys_text(env)
        for kind in BOUNDARY_KINDS:
            assert _boundary_helper(env, kind).exists(), kind
            assert env.ct_file(FAKE_VMID, boundary_key_ct_path(kind)).exists(), kind
            assert f"hubinet-ops-package-{kind}-vmid-{FAKE_VMID}-" in authorized, kind
        # The scan boundary and the operator's own key both survive intact.
        assert unrelated in authorized
        assert f"hubinet-ops-package-scan-vmid-{FAKE_VMID}-{FAKE_RUN_ID}" in authorized
        inventory = env.ct_file_text(FAKE_VMID, "/etc/hubinet-ops/inventory.yaml")
        assert "package_update:" in inventory
        assert "enabled: true" in inventory
        # And the pre-existing configuration is preserved byte-for-byte above
        # the appended block.
        assert inventory.startswith('source:\n  display_name: "Home Proxmox"')
        assert "package_scan:" in inventory.split("package_update:", 1)[0]
        # The five boundaries reach the SAME endpoint the scan boundary
        # already reaches -- one configured source, one SSH endpoint. Only
        # the private keys differ.
        activation = inventory.split("package_update:", 1)[1]
        assert 'host: "192.0.2.10"' in activation
        assert 'known_hosts_path: "/etc/hubinet-ops/host-control/known_hosts"' in activation
        for kind in BOUNDARY_KINDS:
            assert f'{kind}_private_key_path: "{boundary_key_ct_path(kind)}"' in activation
        for journal in UPDATE_BOUNDARY_JOURNAL_DIRS:
            assert (_host_root(env) / journal.lstrip("/")).is_dir(), journal

    def test_a_failed_upgrade_leaves_no_new_privileged_access_path(self, tmp_path):
        """The rule this whole module exists for.

        A product update that created a key, an `authorized_keys` entry, and
        a root-owned mutation helper and then failed must remove all three --
        and only those three.
        """

        env = seed_installed_environment(
            tmp_path,
            installed_source_sha="1" * 40,
            activated=False,
            scenario_overrides={"discovery_result": "backend_unreachable"},
        )
        unrelated = "ssh-ed25519 QUFBQUFBQUFBQUFBQUFBQUFBQUFB pve-operator\n"
        authorized_path = _host_root(env) / "root" / ".ssh" / "authorized_keys"
        before_authorized = authorized_path.read_text(encoding="utf-8") + unrelated
        authorized_path.write_text(before_authorized, encoding="utf-8")
        before_inventory = env.ct_file_text(
            FAKE_VMID, "/etc/hubinet-ops/inventory.yaml"
        )
        target = build_update_target_checkout(tmp_path / "target", REPO_ROOT)

        result = _run(env.env, _base_args(target))

        assert result.returncode != 0
        authorized = _authorized_keys_text(env)
        for kind in BOUNDARY_KINDS:
            assert not _boundary_helper(env, kind).exists(), kind
            assert not env.ct_file(
                FAKE_VMID, boundary_key_ct_path(kind)
            ).exists(), kind
            assert f"hubinet-ops-package-{kind}-vmid-{FAKE_VMID}-" not in authorized
        # Nothing else was disturbed: the operator's key, the scan boundary,
        # and the pre-activation configuration are all exactly as they were.
        assert authorized == before_authorized
        restored = env.ct_file_text(FAKE_VMID, "/etc/hubinet-ops/inventory.yaml")
        assert restored == before_inventory
        # The property behind that byte comparison, stated directly: a
        # configuration left activating the lifecycle while naming the five
        # keys this rollback just deleted would fail the restored service's
        # own startup closed -- so a failed update would not merely fail, it
        # would leave the installation unable to come back.
        assert "package_update:" not in restored

    def test_a_rollback_that_cannot_restore_the_config_hard_stops(self, tmp_path):
        """The positive control for the rule above.

        If the pre-activation configuration cannot be put back, the updater
        must NOT go on to delete the key material that configuration still
        names. It hard stops, preserves every artifact and the active
        journal, and says so -- manual recovery is strictly safer than an
        installation whose service can no longer start.
        """

        env = seed_installed_environment(
            tmp_path,
            installed_source_sha="1" * 40,
            activated=False,
            scenario_overrides={
                "discovery_result": "backend_unreachable",
                "fail": ["cp_rollback_config_to_live"],
            },
        )
        target = build_update_target_checkout(tmp_path / "target", REPO_ROOT)

        result = _run(env.env, _base_args(target))

        assert result.returncode != 0
        assert "ROLLBACK COULD NOT BE COMPLETED" in result.stderr
        assert "pre-activation" in result.stderr
        # It stopped BEFORE deleting the keys the live configuration names.
        for kind in BOUNDARY_KINDS:
            assert env.ct_file(FAKE_VMID, boundary_key_ct_path(kind)).exists(), kind
        # And it preserved the journal for the operator rather than clearing it.
        journal = _update_state_path(env, FAKE_VMID, "journal")
        assert journal.exists()

    def test_a_failed_upgrade_preserves_an_existing_journal_directory(
        self, tmp_path
    ):
        """A journal this run did not create is never removed to tidy up.

        It may hold another operation's durable at-most-once evidence, and
        destroying that would be strictly worse than leaving a directory.
        """

        env = seed_installed_environment(
            tmp_path,
            installed_source_sha="1" * 40,
            activated=False,
            scenario_overrides={"discovery_result": "backend_unreachable"},
        )
        journal = _host_root(env) / "var/lib/hubinet-ops/rollback-operations"
        journal.mkdir(parents=True, exist_ok=True)
        (journal / "evidence.json").write_text("{}", encoding="utf-8")
        target = build_update_target_checkout(tmp_path / "target", REPO_ROOT)

        result = _run(env.env, _base_args(target))

        assert result.returncode != 0
        assert (journal / "evidence.json").exists()


class TestActiveWorkloadJobRefusesTheUpdater:
    """The activation invariant: an in-flight workload update fences this."""

    def test_an_active_job_refuses_before_any_mutation(self, tmp_path):
        env = seed_installed_environment(
            tmp_path,
            installed_source_sha="1" * 40,
            scenario_overrides={"update_probe_package_update_active": True},
        )
        before_authorized = _authorized_keys_text(env)
        before_unit = env.ct_file_text(
            FAKE_VMID, "/etc/systemd/system/hubinet-ops.service"
        )
        state_before = env.state()
        target = build_update_target_checkout(tmp_path / "target", REPO_ROOT)

        result = _run(env.env, _base_args(target))

        assert result.returncode != 0
        assert "refusing to update: package update job" in result.stderr
        # Refused in Phase U2, so nothing was staged, stopped, or replaced.
        assert "Phase U3" not in result.stdout
        assert _authorized_keys_text(env) == before_authorized
        assert env.ct_file_text(
            FAKE_VMID, "/etc/systemd/system/hubinet-ops.service"
        ) == before_unit
        assert env.state()["vmids"][FAKE_VMID]["service"] == "active"
        assert state_before["vmids"][FAKE_VMID] == env.state()["vmids"][FAKE_VMID]

    def test_no_active_job_proceeds_normally(self, tmp_path):
        env = seed_installed_environment(tmp_path, installed_source_sha="1" * 40)
        target = build_update_target_checkout(tmp_path / "target", REPO_ROOT)

        result = _run(env.env, _base_args(target))

        assert result.returncode == 0, result.stderr
        assert "Active workload update job:    none" in result.stdout

    def test_an_unanswerable_active_job_question_also_refuses(self, tmp_path):
        """"We could not ask" is never read as "the answer was no"."""

        env = seed_installed_environment(
            tmp_path,
            installed_source_sha="1" * 40,
            scenario_overrides={
                "update_probe_package_update_unavailable": True
            },
        )
        target = build_update_target_checkout(tmp_path / "target", REPO_ROOT)

        result = _run(env.env, _base_args(target))

        assert result.returncode != 0
        assert "pre-update live probe failed" in result.stderr

    def test_a_pre_activation_backend_without_the_route_is_not_fenced(
        self, tmp_path
    ):
        """A backend that cannot own a workload job has nothing to fence.

        Read from the endpoint's real 404, never from a transport failure.
        """

        env = seed_installed_environment(
            tmp_path,
            installed_source_sha="1" * 40,
            activated=False,
            scenario_overrides={"update_probe_package_update_absent": True},
        )
        target = build_update_target_checkout(tmp_path / "target", REPO_ROOT)

        result = _run(env.env, _base_args(target))

        assert result.returncode == 0, result.stderr
