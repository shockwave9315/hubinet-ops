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
