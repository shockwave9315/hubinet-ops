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

import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap_fake_pve import git_head_sha  # noqa: E402
from _update_fake_pve import (  # noqa: E402
    FAKE_BACKEND_INSTANCE_ID,
    FAKE_RUN_ID,
    FAKE_VMID,
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


def _base_args(target_dir, *, vmid=FAKE_VMID, expected_sha=None, extra=()):
    args = ["--vmid", vmid, "--source-dir", str(target_dir), "--non-interactive", "--yes"]
    if expected_sha is None:
        expected_sha = git_head_sha(target_dir)
    if expected_sha is not False:
        args += ["--expected-sha", expected_sha]
    return args + list(extra)


@pytest.fixture
def target_checkout(tmp_path):
    return build_update_target_checkout(tmp_path / "target", REPO_ROOT, schema_version=8)


# ---------------------------------------------------------------------------
# A. Code-only update -- the most important long-term case. Must be boring.
# ---------------------------------------------------------------------------


class TestCodeOnlyUpdate:
    def test_boring_update_preserves_everything(self, tmp_path):
        env = seed_installed_environment(
            tmp_path, schema_version=8, installed_source_sha="1" * 40
        )
        pre_state = env.state()
        pre_authorized_keys = (
            Path(env.env["HUBINET_OPS_TEST_HOST_ROOT"]) / "root" / ".ssh" / "authorized_keys"
        ).read_text(encoding="utf-8")
        pre_helper = env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/.venv/bin/pip")
        pre_agent_env = env.ct_file_text(FAKE_VMID, "/etc/hubinet-ops/agent.env")
        pre_nft = env.ct_file_text(FAKE_VMID, "/etc/nftables.conf")

        target = build_update_target_checkout(tmp_path / "target-boring", REPO_ROOT, schema_version=8)
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
        assert db["schema_version"] == 8
        # Installed-source marker recorded.
        marker = env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/.hubinet-source-commit").strip()
        assert marker == git_head_sha(target)
        assert "backend_instance_id" in result.stdout or "backend_instance_id" in result.stderr


# ---------------------------------------------------------------------------
# B. requirements.txt change -- new venv staged and swapped, never in-place.
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

    def test_failed_venv_staging_leaves_old_service_untouched(self, tmp_path):
        env = seed_installed_environment(
            tmp_path,
            installed_requirements="fastapi==0.100.0\n",
            scenario_overrides={"fail": ["pip_install"]},
        )
        target = build_update_target_checkout(
            tmp_path / "target-reqs-fail", REPO_ROOT, requirements_text="fastapi==0.116.1\n"
        )
        result = _run(env.env, _base_args(target))
        assert result.returncode != 0
        assert "the ACTIVE virtualenv was never touched" in result.stderr
        assert env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/requirements.txt") == "fastapi==0.100.0\n"
        assert not any(
            line.startswith("systemctl") and "stop" in line for line in env.log_lines()
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
            tmp_path, schema_version=7,
            scenario_overrides={"discovery_backend_instance_id": new_backend_instance_id},
        )
        target = build_update_target_checkout(tmp_path / "target-v8", REPO_ROOT, schema_version=8)
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
        assert backup_data["schema_version"] == 7

    def test_reset_refused_without_allow_flag_makes_zero_mutation(self, tmp_path):
        env = seed_installed_environment(tmp_path, schema_version=7)
        target = build_update_target_checkout(tmp_path / "target-v8-refused", REPO_ROOT, schema_version=8)
        result = _run(env.env, _base_args(target))
        assert result.returncode != 0
        assert "--allow-authority-reset" in result.stderr
        db = json.loads(env.ct_file_text(FAKE_VMID, "/var/lib/hubinet-ops/authority.db"))
        assert db["schema_version"] == 7
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
            schema_version=7,
            scenario_overrides={"discovery_result": "backend_unreachable"},
        )
        target = build_update_target_checkout(tmp_path / "target-v8-fail", REPO_ROOT, schema_version=8)
        result = _run(env.env, _base_args(target, extra=["--allow-authority-reset"]))
        assert result.returncode != 0

        db = json.loads(env.ct_file_text(FAKE_VMID, "/var/lib/hubinet-ops/authority.db"))
        assert db["schema_version"] == 7, "the OLD authority database must be restored, not a new schema-8 one"
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

        target_b = build_update_target_checkout(
            tmp_path / "target-b", REPO_ROOT, requirements_text="fastapi==0.200.0\n"
        )
        result_b = _run(env.env, _base_args(target_b))
        assert result_b.returncode == 0, result_b.stderr
        sha_b = env.ct_file_text(FAKE_VMID, "/opt/hubinet-ops/.hubinet-source-commit").strip()
        assert sha_b == git_head_sha(target_b)
        assert sha_b != sha_a

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

    def test_venv_second_move_failure_restores_old_venv(self, tmp_path):
        env = seed_installed_environment(
            tmp_path,
            installed_requirements="fastapi==0.100.0\n",
            scenario_overrides={"fail": ["mv_staged_venv_to_live"]},
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
            schema_version=7,
            scenario_overrides={
                "discovery_result": "backend_unreachable",
                "fail_nth_authority_remove": 2,
            },
        )
        target = build_update_target_checkout(tmp_path / "target-v8-remove-fail", REPO_ROOT, schema_version=8)
        result = _run(env.env, _base_args(target, extra=["--allow-authority-reset"]))
        assert result.returncode != 0
        assert "ROLLBACK COULD NOT BE COMPLETED" in result.stderr
        assert "could not prove removal" in result.stderr
        backups_root = env.ct_file(FAKE_VMID, "/var/lib/hubinet-ops/update-backups")
        backup_files = list(backups_root.rglob("authority.db"))
        assert backup_files, "expected the retained authority DB backup to survive"
        backup_data = json.loads(backup_files[0].read_text(encoding="utf-8"))
        assert backup_data["schema_version"] == 7
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
            schema_version=7,
            scenario_overrides={"fail_nth_authority_remove_partial": 1},
        )
        target = build_update_target_checkout(tmp_path / "target-v8-partial-remove", REPO_ROOT, schema_version=8)
        result = _run(env.env, _base_args(target, extra=["--allow-authority-reset"]))
        assert result.returncode != 0

        db = json.loads(env.ct_file_text(FAKE_VMID, "/var/lib/hubinet-ops/authority.db"))
        assert db["schema_version"] == 7, (
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
            schema_version=8,
            # Missing "one_active_endpoint_per_source" relative to the
            # target's default required set (FAKE_REQUIRED_SCHEMA_OBJECTS).
            schema_objects=["authority_schema", "backend_instance"],
        )
        target = build_update_target_checkout(tmp_path / "target-schema-drift", REPO_ROOT, schema_version=8)
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
