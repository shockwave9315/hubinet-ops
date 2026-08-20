"""Sandboxed smoke tests for deploy/bootstrap-proxmox-0.5.sh.

THE ONLY test file in this repository that executes
deploy/bootstrap-proxmox-0.5.sh as a real subprocess. Per AGENTS.md's
deployment-script sandbox boundary, that makes this file's execution
restricted to the Docker-based ephemeral-CI sandbox
(tests/shell/run_bootstrap_smoke_sandbox.sh) -- every test below is a hard
pytest skip anywhere else, checked via the HUBINET_OPS_SYSTEM_SANDBOX
marker that sandbox sets. Running `pytest tests/` on a normal developer
machine or in ordinary (non-ephemeral-marked) CI collects these tests but
skips every one of them with a clear reason -- it never silently executes
the real script outside the sandbox, and it never reports a false pass.

Even inside the sandbox, every `pct`/`pveum`/`pveam`/`pvesh`/`pvesm`/`nft`
invocation the script makes is intercepted by the hermetic fake-command
layer in tests/_bootstrap_fake_pve.py -- no real PVE/network/HA endpoint
is ever contacted, even from inside the already network-isolated
container. `git` is deliberately the real system binary (see that
module's docstring) since Mandatory Fix 4/6 is specifically about real
git provenance behavior.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap_fake_pve import (  # noqa: E402
    FAKE_DISPLAY_NAME,
    FAKE_HA_SOURCE_CIDR,
    FAKE_PVE_ENDPOINT,
    FAKE_PVE_ENDPOINT_HOST,
    build_fake_pve_environment,
    build_minimal_source_checkout,
    default_scenario,
    git_head_sha,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_SCRIPT = REPO_ROOT / "deploy" / "bootstrap-proxmox-0.5.sh"

pytestmark = [
    pytest.mark.skipif(
        __import__("os").environ.get("HUBINET_OPS_SYSTEM_SANDBOX") != "1",
        reason=(
            "this file executes the real bootstrap script and per AGENTS.md's "
            "deployment-script sandbox boundary only ever runs inside "
            "tests/shell/run_bootstrap_smoke_sandbox.sh (ephemeral-CI-only, "
            "Docker-isolated) -- see that script and "
            "tests/test_bootstrap_proxmox_0_5.py::test_smoke_suite_is_sandbox_gated"
        ),
    ),
    pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not available"),
]


def _run(env, args, *, source_dir=None, timeout=30, input_text=None):
    argv = ["bash", str(BOOTSTRAP_SCRIPT), *args]
    if source_dir is not None:
        argv += ["--source-dir", str(source_dir)]
    return subprocess.run(
        argv,
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        input=input_text,
    )


def _base_args(source_dir, **overrides):
    # --tls-trust system is a default here purely so tests NOT specifically
    # about TLS behavior (the large majority of this file) can reach a
    # full successful run without needing real PVE CA material at
    # /etc/pve/pve-root-ca.pem (a real host filesystem path this hermetic
    # harness has no way to fake -- see TestTls, which explicitly passes
    # base_overrides={"--tls-trust": False} to test the CA-bundle and
    # missing-trust-material paths on their own terms instead).
    args = {
        "--non-interactive": None,
        "--yes": None,
        "--ha-source": FAKE_HA_SOURCE_CIDR,
        "--pve-endpoint": FAKE_PVE_ENDPOINT,
        "--storage": "local-lxc",
        "--bridge": "vmbr0",
        "--tls-trust": "system",
    }
    # Lazy: only actually shells out to `git rev-parse HEAD` when the
    # caller hasn't overridden --expected-sha away (e.g. to False) --
    # some callers (non-git-source tests) pass a source_dir that is
    # deliberately not a git checkout at all, where git_head_sha() itself
    # would raise.
    if overrides.get("--expected-sha", True) is not False:
        args["--expected-sha"] = git_head_sha(source_dir)
    args.update(overrides)
    flat: list[str] = []
    for key, value in args.items():
        if value is None:
            flat.append(key)
        elif value is False:
            continue
        else:
            flat += [key, str(value)]
    return flat


@pytest.fixture(scope="session")
def source_checkout(tmp_path_factory):
    return build_minimal_source_checkout(tmp_path_factory.mktemp("bootstrap-src"), REPO_ROOT)


@pytest.fixture
def fake_env(tmp_path):
    return build_fake_pve_environment(tmp_path, default_scenario())


def _run_full(tmp_path, source_checkout, *, args=(), scenario_overrides=None, base_overrides=None):
    scenario = default_scenario()
    if scenario_overrides:
        scenario.update(scenario_overrides)
    fake_env_obj = build_fake_pve_environment(tmp_path, scenario)
    full_args = _base_args(source_checkout, **(base_overrides or {})) + list(args)
    return _run(fake_env_obj.env, full_args, source_dir=source_checkout), fake_env_obj


# ---------------------------------------------------------------------------
# VMID: auto-detect, explicit override, race, never-destroy
# ---------------------------------------------------------------------------


class TestVmidSelection:
    def test_auto_detected_when_not_given(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={"next_free_vmid": "137"},
        )
        assert result.returncode == 0, result.stderr
        assert "auto-detected next free VMID: 137" in result.stderr
        assert any("pct create 137" in line for line in fake_env_obj.log_lines())

    def test_explicit_vmid_is_honored_and_never_overridden(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            args=["--vmid", "222"],
            scenario_overrides={"next_free_vmid": "999"},  # must be ignored
        )
        assert result.returncode == 0, result.stderr
        assert any("pct create 222" in line for line in fake_env_obj.log_lines())
        assert not any("pct create 999" in line for line in fake_env_obj.log_lines())

    def test_explicit_vmid_already_existing_is_a_hard_stop(self, tmp_path, source_checkout):
        scenario = default_scenario()
        fake_env_obj = build_fake_pve_environment(tmp_path, scenario)
        state_path = fake_env_obj.state_path
        state = json.loads(state_path.read_text())
        state["vmids"]["222"] = {"started": False, "onboot": "0", "features": ""}
        state_path.write_text(json.dumps(state))
        result = _run(fake_env_obj.env, _base_args(source_checkout) + ["--vmid", "222"], source_dir=source_checkout)
        assert result.returncode != 0
        assert "already exists" in result.stderr
        assert not any(line.startswith("pct destroy") or line.startswith("pct create") for line in fake_env_obj.log_lines())

    def test_auto_detected_vmid_race_is_recomputed_not_fatal(self, tmp_path, source_checkout):
        # First candidate (110) is claimed between planning and creation;
        # the second call to /cluster/nextid (111) must be used instead --
        # never treated as a hard failure, since no operator ever
        # committed to the specific auto-detected number.
        scenario = default_scenario()
        scenario["next_free_vmid"] = ["110", "111"]
        fake_env_obj = build_fake_pve_environment(tmp_path, scenario)
        state = json.loads(fake_env_obj.state_path.read_text())
        state["vmids"]["110"] = {"started": False, "onboot": "0", "features": ""}
        fake_env_obj.state_path.write_text(json.dumps(state))
        result = _run(fake_env_obj.env, _base_args(source_checkout), source_dir=source_checkout)
        assert result.returncode == 0, result.stderr
        assert "recomputing" in result.stderr
        assert any("pct create 111" in line for line in fake_env_obj.log_lines())

    def test_failure_never_destroys_a_preexisting_vmid(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            args=["--vmid", "333", "--cleanup-on-failure"],
            scenario_overrides={"fail": ["pveum_user_add"]},
        )
        assert result.returncode != 0
        # A conflicting pre-existing VMID is never even reached in this
        # scenario (failure occurs at PVE identity creation, after the CT
        # created by THIS run); ensure no destroy of any VMID other than
        # the one this run itself created is issued.
        destroy_lines = [line for line in fake_env_obj.log_lines() if "pct destroy" in line]
        for line in destroy_lines:
            assert "333" in line


# ---------------------------------------------------------------------------
# Plan ordering: no mutation before confirmation
# ---------------------------------------------------------------------------


class TestPlanOrdering:
    def test_pveam_update_never_runs_before_confirmation_when_declined(self, tmp_path, source_checkout):
        scenario = default_scenario()
        scenario["local_templates"] = []  # forces a would-be download path
        fake_env_obj = build_fake_pve_environment(tmp_path, scenario)
        args = _base_args(source_checkout, **{"--non-interactive": False, "--yes": False})
        # Interactive mode, decline the confirmation prompt.
        result = _run(fake_env_obj.env, args, source_dir=source_checkout, input_text="n\n")
        assert result.returncode != 0
        assert not any(line.startswith("pveam update") or line.startswith("pveam download") for line in fake_env_obj.log_lines())
        assert not any(line.startswith("pct create") for line in fake_env_obj.log_lines())

    def test_no_pct_or_pveum_mutation_before_confirmation(self, tmp_path, source_checkout):
        fake_env_obj = build_fake_pve_environment(tmp_path, default_scenario())
        args = _base_args(source_checkout, **{"--non-interactive": False, "--yes": False})
        result = _run(fake_env_obj.env, args, source_dir=source_checkout, input_text="n\n")
        assert result.returncode != 0
        assert "aborted by operator" in result.stderr
        for line in fake_env_obj.log_lines():
            assert not line.startswith("pct create")
            assert not line.startswith("pveum user add")
            assert not line.startswith("pveum role add")

    def test_plan_shows_vmid_template_and_source_commit(self, tmp_path, source_checkout):
        result, _ = _run_full(tmp_path, source_checkout)
        assert result.returncode == 0, result.stderr
        assert "Plan: create VMID" in result.stderr
        assert "source commit" in result.stderr
        assert git_head_sha(source_checkout) in result.stderr


# ---------------------------------------------------------------------------
# Source provenance (Mandatory Fix 4/6)
# ---------------------------------------------------------------------------


class TestSourceProvenance:
    def test_non_git_source_dir_rejected(self, tmp_path, fake_env):
        non_git = tmp_path / "not-a-repo"
        (non_git / "app").mkdir(parents=True)
        (non_git / "deploy").mkdir(parents=True)
        (non_git / "app" / "__init__.py").write_text("", encoding="utf-8")
        (non_git / "requirements.txt").write_text("x==1\n", encoding="utf-8")
        (non_git / "deploy" / "install-0.5.0-fresh.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        result = _run(fake_env.env, _base_args(non_git, **{"--expected-sha": False}), source_dir=non_git)
        assert result.returncode != 0
        assert "is not a git checkout" in result.stderr

    def test_dirty_worktree_rejected(self, tmp_path, source_checkout, fake_env):
        (source_checkout / "requirements.txt").write_text("dirty==1\n", encoding="utf-8")
        try:
            result = _run(fake_env.env, _base_args(source_checkout), source_dir=source_checkout)
            assert result.returncode != 0
            assert "dirty working tree" in result.stderr
        finally:
            subprocess.run(
                ["git", "-C", str(source_checkout), "checkout", "--", "requirements.txt"],
                check=True, capture_output=True,
            )

    def test_expected_sha_mismatch_rejected(self, tmp_path, source_checkout, fake_env):
        result = _run(
            fake_env.env,
            _base_args(source_checkout, **{"--expected-sha": "0" * 40}),
            source_dir=source_checkout,
        )
        assert result.returncode != 0
        assert "does not match --expected-sha" in result.stderr

    def test_non_interactive_requires_expected_sha(self, tmp_path, source_checkout, fake_env):
        result = _run(fake_env.env, _base_args(source_checkout, **{"--expected-sha": False}), source_dir=source_checkout)
        assert result.returncode != 0
        assert "--non-interactive requires --expected-sha" in result.stderr

    def test_exact_confirmed_sha_is_archived_and_logged(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(tmp_path, source_checkout)
        assert result.returncode == 0, result.stderr
        sha = git_head_sha(source_checkout)
        assert f"Deploying source commit: {sha}" in result.stderr
        # run_logged's own trace line is "+ git -C <source_dir> archive
        # <sha> -o <tarball>" -- assert on the part after "-C <dir>"
        # rather than requiring "git archive" to be adjacent, since the
        # real command line always has "-C <source_dir>" in between.
        assert f"archive {sha} -o" in result.stderr

    def test_untracked_file_present_aborts_as_dirty_so_it_is_never_transferred(self, tmp_path, source_checkout):
        # `git status --porcelain` (the dirty-worktree check `_plan_source_
        # commit` uses) reports untracked files too -- so an untracked
        # file never gets a chance to be silently excluded from the
        # archive: the whole run refuses to start at all. This is a
        # stronger guarantee than "excluded from the tarball" would be,
        # and is asserted directly here rather than via a "silent
        # exclusion" test that would never actually be reached.
        untracked = source_checkout / "untracked-secret.txt"
        untracked.write_text("should-never-be-shipped\n", encoding="utf-8")
        try:
            result, fake_env_obj = _run_full(tmp_path, source_checkout)
            assert result.returncode != 0
            assert "dirty working tree" in result.stderr
            assert not any(line.startswith("pct create") for line in fake_env_obj.log_lines())
        finally:
            untracked.unlink(missing_ok=True)

    def test_git_archive_of_a_clean_tracked_tree_excludes_untracked_content(self, tmp_path, source_checkout):
        # Positive control for the property above, without going through
        # the bootstrap script: `git archive <sha>` itself (the exact
        # mechanism phase 8 uses) never includes untracked files, on a
        # clean worktree with no state to abort on.
        untracked = source_checkout / "untracked-secret.txt"
        untracked.write_text("should-never-be-shipped\n", encoding="utf-8")
        try:
            sha = git_head_sha(source_checkout)
            archive_check = subprocess.run(
                ["git", "-C", str(source_checkout), "archive", sha, "-o", str(tmp_path / "check.tar")],
                capture_output=True,
            )
            assert archive_check.returncode == 0
            listing = subprocess.run(
                ["tar", "-tf", str(tmp_path / "check.tar")], capture_output=True, text=True
            )
            assert "untracked-secret.txt" not in listing.stdout
        finally:
            untracked.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# PVE identity / exact permission set (Mandatory Fix 2)
# ---------------------------------------------------------------------------


class TestPveIdentity:
    def test_exact_pair_passes(self, tmp_path, source_checkout):
        result, _ = _run_full(tmp_path, source_checkout)
        assert result.returncode == 0, result.stderr
        assert "PASS: PVE identity" in result.stderr

    @pytest.mark.parametrize(
        "extra_priv",
        ["VM.Config.Disk", "VM.Migrate", "VM.Clone", "VM.Snapshot", "VM.Snapshot.Rollback", "VM.Console", "Sys.Console"],
    )
    def test_any_extra_privilege_fails_closed(self, tmp_path, source_checkout, extra_priv):
        result, _ = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={"role_privs_override": {"HubinetOpsR0Auditor": ["Sys.Audit", "VM.Audit", extra_priv]}},
        )
        assert result.returncode != 0
        assert "verification failed" in result.stderr
        assert extra_priv in result.stderr

    def test_missing_required_privilege_fails_closed(self, tmp_path, source_checkout):
        result, _ = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={"role_privs_override": {"HubinetOpsR0Auditor": ["Sys.Audit"]}},
        )
        assert result.returncode != 0
        assert "verification failed" in result.stderr

    def test_permissions_are_acl_state_derived_not_a_fixed_scenario_value(self, tmp_path, source_checkout):
        # No token_permissions/role_privs_override at all: the fake derives
        # the result purely from the ACL-grant/role-privs it actually
        # recorded during THIS run's own pveum calls.
        result, fake_env_obj = _run_full(tmp_path, source_checkout)
        assert result.returncode == 0, result.stderr
        state = fake_env_obj.state()
        assert state["pve_roles"]["HubinetOpsR0Auditor"] == ["Sys.Audit", "VM.Audit"]
        assert any(g["role"] == "HubinetOpsR0Auditor" and g["target"].startswith("token:") for g in state["acl_grants"])

    def test_conflict_refusal_existing_user(self, tmp_path, source_checkout):
        scenario = default_scenario()
        fake_env_obj = build_fake_pve_environment(tmp_path, scenario)
        state = json.loads(fake_env_obj.state_path.read_text())
        state["pve_users"].append("hubinetops@pve")
        fake_env_obj.state_path.write_text(json.dumps(state))
        result = _run(fake_env_obj.env, _base_args(source_checkout), source_dir=source_checkout)
        assert result.returncode != 0
        assert "already exists" in result.stderr
        assert not any(line.startswith("pveum user delete") for line in fake_env_obj.log_lines())

    def test_token_secret_never_in_stdout_stderr_or_log(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(tmp_path, source_checkout)
        assert result.returncode == 0, result.stderr
        secret = default_scenario()["pve_token_secret"]
        assert secret not in result.stdout
        assert secret not in result.stderr
        for line in fake_env_obj.log_lines():
            assert secret not in line


# ---------------------------------------------------------------------------
# Secrets: no argv exposure anywhere (Mandatory Fix 5)
# ---------------------------------------------------------------------------


class TestSecretHandling:
    def test_pve_token_never_appears_as_a_logged_argv_element(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(tmp_path, source_checkout)
        assert result.returncode == 0, result.stderr
        secret = default_scenario()["pve_token_secret"]
        # The fake command log records every argv element the script
        # passed to pct/pveum/pveam/pvesh/pvesm/nft -- this is the
        # strongest available proxy for "never appeared in this process's
        # own argv" inside this hermetic harness.
        for line in fake_env_obj.log_lines():
            assert secret not in line

    def test_r0_api_bearer_token_never_appears_as_a_logged_argv_element(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(tmp_path, source_checkout)
        assert result.returncode == 0, result.stderr
        token = default_scenario()["r0_api_token"]
        for line in fake_env_obj.log_lines():
            assert token not in line
        assert token not in result.stdout
        assert token not in result.stderr

    def test_agent_env_written_with_pve_token_value(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(tmp_path, source_checkout)
        assert result.returncode == 0, result.stderr
        agent_env = fake_env_obj.ct_file_text("110", "/etc/hubinet-ops/agent.env")
        secret = default_scenario()["pve_token_secret"]
        assert f"=hubinetops@pve!r0-readonly={secret}" in agent_env

    def test_secret_temp_files_are_cleaned_up_on_success(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(tmp_path, source_checkout)
        assert result.returncode == 0, result.stderr
        leftover = list(Path("/tmp").glob("hubinet-ops-bootstrap-*"))
        assert leftover == [] or all(not p.exists() for p in leftover)

    def test_secret_temp_files_are_cleaned_up_on_failure(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout, scenario_overrides={"fail": ["pveum_token_add"]}
        )
        assert result.returncode != 0
        leftover = list(Path("/tmp").glob("hubinet-ops-bootstrap-*secret*"))
        assert leftover == [] or all(not p.exists() for p in leftover)


# ---------------------------------------------------------------------------
# Rollback: partial-success cleanup, onboot never left enabled, pre-existing
# resources never touched
# ---------------------------------------------------------------------------


class TestRollback:
    def test_service_disabled_even_when_enable_reports_failure_after_mutating(self, tmp_path, source_checkout):
        # Simulates the exact partial-success hole: systemctl "succeeds"
        # (state mutated) is not what's being tested here directly (the
        # fake's service_enable failure returns before mutating state) --
        # instead this proves the marker-gated rollback attempts cleanup
        # unconditionally once phase11 was ever entered.
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout, scenario_overrides={"fail": ["service_enable"]}
        )
        assert result.returncode != 0
        assert any("systemctl disable --now hubinet-ops" in line for line in fake_env_obj.log_lines())

    def test_pve_identity_rollback_attempted_even_on_partial_success(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout, scenario_overrides={"fail": ["pveum_acl_modify"]}
        )
        assert result.returncode != 0
        log = fake_env_obj.log_lines()
        assert any(line.startswith("pveum user delete") for line in log)
        assert any(line.startswith("pveum role delete") for line in log)

    def test_onboot_never_enabled_on_failure(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout, scenario_overrides={"fail": ["backend_health"]}
        )
        assert result.returncode != 0
        state = fake_env_obj.state()
        assert state["vmids"]["110"]["onboot"] == "0"

    def test_preserve_on_failure_is_the_default(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout, scenario_overrides={"fail": ["backend_health"]}
        )
        assert result.returncode != 0
        assert not any(line.startswith("pct destroy") for line in fake_env_obj.log_lines())
        state = fake_env_obj.state()
        assert "110" in state["vmids"]

    def test_cleanup_on_failure_flag_destroys_container(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            args=["--cleanup-on-failure"],
            scenario_overrides={"fail": ["backend_health"]},
        )
        assert result.returncode != 0
        assert any(line.startswith("pct destroy 110") for line in fake_env_obj.log_lines())

    def test_rollback_never_destroys_a_preexisting_vmid_conflict(self, tmp_path, source_checkout):
        # Explicit --vmid so this hits the immediate phase1 conflict check
        # (VMID_EXPLICIT=1) rather than the auto-detect recompute-on-
        # collision path (a fake /cluster/nextid that always answers the
        # same VMID regardless of state, unlike a real one, would just
        # loop through recompute attempts and die with a different
        # message -- exercised separately by
        # test_auto_detected_vmid_race_is_recomputed_not_fatal instead).
        scenario = default_scenario()
        fake_env_obj = build_fake_pve_environment(tmp_path, scenario)
        state = json.loads(fake_env_obj.state_path.read_text())
        state["vmids"]["110"] = {"started": True, "onboot": "1", "features": ""}
        fake_env_obj.state_path.write_text(json.dumps(state))
        result = _run(
            fake_env_obj.env,
            _base_args(source_checkout) + ["--vmid", "110", "--cleanup-on-failure"],
            source_dir=source_checkout,
        )
        assert result.returncode != 0
        assert "already exists" in result.stderr
        assert not any(line.startswith("pct destroy") for line in fake_env_obj.log_lines())
        after = fake_env_obj.state()
        assert after["vmids"]["110"]["onboot"] == "1"  # untouched


# ---------------------------------------------------------------------------
# TLS trust (Additional C): explicit opt-in only, never implicit
# ---------------------------------------------------------------------------


class TestTls:
    def test_no_verify_false_anywhere_in_generated_config(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(tmp_path, source_checkout)
        assert result.returncode == 0, result.stderr
        inventory = fake_env_obj.ct_file_text("110", "/etc/hubinet-ops/inventory.yaml")
        assert "verify: true" in inventory
        assert "verify: false" not in inventory

    def test_missing_ca_without_explicit_system_trust_fails_closed(self, tmp_path, source_checkout):
        # No --pve-ca-path and no --tls-trust: with no real
        # /etc/pve/pve-root-ca.pem on this host (this hermetic harness has
        # no way to fake an arbitrary host filesystem path lookup), the
        # auto-detected-CA branch always misses here, so this exercises
        # exactly the fail-closed "neither found nor explicitly opted in"
        # path -- the behavior this bootstrap must never bypass silently.
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout, base_overrides={"--tls-trust": False},
        )
        assert result.returncode != 0
        assert "never falls back to system trust implicitly" in result.stderr

    def test_pve_ca_path_and_tls_trust_system_are_mutually_exclusive(self, tmp_path, source_checkout):
        ca_file = tmp_path / "ca.pem"
        ca_file.write_text("-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n", encoding="utf-8")
        result, _ = _run_full(
            tmp_path, source_checkout,
            base_overrides={"--tls-trust": False},
            args=["--pve-ca-path", str(ca_file), "--tls-trust", "system"],
        )
        assert result.returncode != 0
        assert "mutually exclusive" in result.stderr

    def test_explicit_ca_path_is_deployed(self, tmp_path, source_checkout):
        ca_file = tmp_path / "ca.pem"
        ca_file.write_text("-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n", encoding="utf-8")
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            base_overrides={"--tls-trust": False},
            args=["--pve-ca-path", str(ca_file)],
        )
        assert result.returncode == 0, result.stderr
        deployed = fake_env_obj.ct_file_text("110", "/etc/hubinet-ops/pve-ca.pem")
        assert "BEGIN CERTIFICATE" in deployed

    def test_explicit_system_trust_opt_in_works(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(tmp_path, source_checkout)  # base default is --tls-trust system
        assert result.returncode == 0, result.stderr
        assert "explicit operator opt-in" in result.stderr
        assert not fake_env_obj.ct_file(("110"), "/etc/hubinet-ops/pve-ca.pem").exists()

    def test_invalid_tls_trust_value_rejected(self, tmp_path, source_checkout):
        result, _ = _run_full(tmp_path, source_checkout, args=["--tls-trust", "bogus"])
        assert result.returncode != 0
        assert "--tls-trust must be" in result.stderr


# ---------------------------------------------------------------------------
# Tooling provisioning (Additional D)
# ---------------------------------------------------------------------------


class TestToolingProvisioning:
    def test_tooling_installed_before_firewall_and_acceptance(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(tmp_path, source_checkout)
        assert result.returncode == 0, result.stderr
        log = fake_env_obj.log_lines()
        apt_indices = [i for i, line in enumerate(log) if "apt-get" in line]
        nft_push_indices = [i for i, line in enumerate(log) if line.startswith("pct push") and "nftables.conf" in line]
        assert apt_indices, "apt-get was never invoked"
        assert nft_push_indices, "nftables ruleset was never pushed"
        assert max(apt_indices) < min(nft_push_indices)

    def test_missing_tool_after_provisioning_fails_closed(self, tmp_path, source_checkout):
        result, _ = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={"ct_tools_available": ["curl", "ss"]},  # nft missing
        )
        assert result.returncode != 0
        assert "'nft' is still not available" in result.stderr

    def test_apt_get_failure_fails_closed(self, tmp_path, source_checkout):
        result, _ = _run_full(tmp_path, source_checkout, scenario_overrides={"fail": ["apt_get"]})
        assert result.returncode != 0


# ---------------------------------------------------------------------------
# Firewall: exact ingress/egress/skuid/destination/port/order (Additional
# G/H)
# ---------------------------------------------------------------------------


class TestFirewall:
    def test_exact_input_chain_content_and_order(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(tmp_path, source_checkout)
        assert result.returncode == 0, result.stderr
        ruleset = fake_env_obj.ct_file_text("110", "/etc/nftables.conf")
        assert f"ip saddr {FAKE_HA_SOURCE_CIDR} tcp dport 8787 accept" in ruleset
        assert ruleset.index(f"ip saddr {FAKE_HA_SOURCE_CIDR}") < ruleset.index("tcp dport 8787 drop")

    def test_egress_port_derived_from_configured_endpoint_not_hardcoded(self, tmp_path, source_checkout):
        custom_endpoint = f"https://{FAKE_PVE_ENDPOINT_HOST}:9006"
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            args=["--pve-endpoint", custom_endpoint],
        )
        assert result.returncode == 0, result.stderr
        ruleset = fake_env_obj.ct_file_text("110", "/etc/nftables.conf")
        assert "tcp dport 9006 accept" in ruleset
        assert "tcp dport 8006 accept" not in ruleset

    def test_default_port_8006_used_when_endpoint_omits_one(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(tmp_path, source_checkout)
        assert result.returncode == 0, result.stderr
        ruleset = fake_env_obj.ct_file_text("110", "/etc/nftables.conf")
        assert "tcp dport 8006 accept" in ruleset

    def test_dns_rule_required_when_endpoint_is_a_hostname(self, tmp_path, source_checkout):
        result, _ = _run_full(
            tmp_path, source_checkout,
            args=["--pve-endpoint", "https://pve.example.invalid:8006"],
        )
        assert result.returncode != 0
        assert "--dns-resolver" in result.stderr

    def test_dns_rule_scoped_when_endpoint_is_a_hostname(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            args=["--pve-endpoint", "https://pve.example.invalid:8006", "--dns-resolver", "198.51.100.53"],
        )
        assert result.returncode == 0, result.stderr
        ruleset = fake_env_obj.ct_file_text("110", "/etc/nftables.conf")
        assert 'ip daddr 198.51.100.53 udp dport 53 accept' in ruleset

    def test_default_deny_egress_and_ingress_present_and_last(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(tmp_path, source_checkout)
        assert result.returncode == 0, result.stderr
        ruleset = fake_env_obj.ct_file_text("110", "/etc/nftables.conf")
        assert ruleset.rstrip().count('meta skuid "hubinetops" drop') == 1
        assert ruleset.index("tcp dport 8787 accept") < ruleset.index("tcp dport 8787 drop")

    def test_firewall_activated_before_service_start(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(tmp_path, source_checkout)
        assert result.returncode == 0, result.stderr
        log = fake_env_obj.log_lines()
        restart_idx = next(i for i, line in enumerate(log) if "systemctl restart nftables" in line)
        enable_idx = next(i for i, line in enumerate(log) if "enable --now hubinet-ops" in line)
        assert restart_idx < enable_idx

    def test_syntax_validated_before_activation(self, tmp_path, source_checkout):
        result, _ = _run_full(tmp_path, source_checkout, scenario_overrides={"fail": ["nft_syntax"]})
        assert result.returncode != 0
        assert "syntax validation" in result.stderr


# ---------------------------------------------------------------------------
# Discovery acceptance (Mandatory Fix 3): real, contract-grounded PASS
# criteria -- health-only is insufficient, not_yet_observed/failure states
# never PASS, onboot only after a genuine healthy result.
# ---------------------------------------------------------------------------


class TestDiscoveryAcceptance:
    def test_healthy_discovery_yields_pass_and_onboot(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(tmp_path, source_checkout)
        assert result.returncode == 0, result.stderr
        assert "Discovery:            PASS" in result.stdout
        state = fake_env_obj.state()
        assert state["vmids"]["110"]["onboot"] == "1"

    def test_process_health_alone_is_not_sufficient(self, tmp_path, source_checkout):
        # health_body is healthy (curl /r0/v1/health succeeds) but the
        # simulated discovery-accept script reports a timeout -- overall
        # acceptance must still fail and onboot must never be enabled.
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={"discovery_result": "timeout"},
        )
        assert result.returncode != 0
        assert "discovery" in result.stderr.lower()
        state = fake_env_obj.state()
        assert state["vmids"]["110"]["onboot"] == "0"

    @pytest.mark.parametrize(
        "terminal_health", ["terminal:source_unavailable", "terminal:configuration_error"]
    )
    def test_terminal_failure_health_never_passes(self, tmp_path, source_checkout, terminal_health):
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={"discovery_result": terminal_health},
        )
        assert result.returncode != 0
        state = fake_env_obj.state()
        assert state["vmids"]["110"]["onboot"] == "0"

    def test_backend_instance_id_missing_fails(self, tmp_path, source_checkout):
        result, _ = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={"discovery_backend_instance_id": ""},
        )
        assert result.returncode != 0

    def test_source_name_mismatch_fails(self, tmp_path, source_checkout):
        result, _ = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={"discovery_source_name": "Some Other Proxmox"},
        )
        assert result.returncode != 0
        assert "source-name-mismatch" in result.stderr

    def test_resource_state_level_violation_fails(self, tmp_path, source_checkout):
        result, _ = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={"discovery_result": "resource_state_level_violation"},
        )
        assert result.returncode != 0

    def test_resource_security_continuity_trusted_fails(self, tmp_path, source_checkout):
        result, _ = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={"discovery_result": "resource_security_continuity_violation"},
        )
        assert result.returncode != 0

    def test_resource_effective_capabilities_present_fails(self, tmp_path, source_checkout):
        result, _ = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={"discovery_result": "resource_capabilities_violation"},
        )
        assert result.returncode != 0

    def test_zero_resources_on_healthy_source_is_legitimate(self, tmp_path, source_checkout):
        # Explicitly NOT a failure: a fresh install may legitimately
        # discover zero resources on the very first cycle.
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={"discovery_resource_count": 0},
        )
        assert result.returncode == 0, result.stderr

    def test_custom_discovery_timeout_flag_is_honored(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            args=["--discovery-timeout", "5"],
        )
        assert result.returncode == 0, result.stderr
        # The flag is consumed by bash argument parsing, never forwarded
        # as a literal argument to any pct/pveum/pveam/pvesh/pvesm/nft call.
        assert not any("--discovery-timeout" in line for line in fake_env_obj.log_lines())


# ---------------------------------------------------------------------------
# Legacy-absence / acceptance basics retained from the original suite
# ---------------------------------------------------------------------------


class TestAcceptanceBasics:
    def test_legacy_ops_db_presence_blocks_acceptance(self, tmp_path, source_checkout):
        result, _ = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={"legacy_present": {"ops_db": True}},
        )
        assert result.returncode != 0
        assert "ops.db" in result.stderr

    def test_legacy_hostd_presence_blocks_acceptance(self, tmp_path, source_checkout):
        result, _ = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={"legacy_present": {"hostd": True}},
        )
        assert result.returncode != 0

    def test_legacy_hostd_port_blocks_acceptance(self, tmp_path, source_checkout):
        result, _ = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={"legacy_present": {"hostd_port": True}},
        )
        assert result.returncode != 0

    def test_failed_systemd_units_block_acceptance(self, tmp_path, source_checkout):
        result, _ = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={"failed_units": ["dev-mqueue.mount loaded failed failed"]},
        )
        assert result.returncode != 0

    def test_installer_failure_stops_before_config_or_firewall(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(tmp_path, source_checkout, scenario_overrides={"fail": ["installer"]})
        assert result.returncode != 0
        assert not any(line.startswith("pct push") and "nftables.conf" in line for line in fake_env_obj.log_lines())
        assert not any(line.startswith("pct push") and "inventory.yaml" in line for line in fake_env_obj.log_lines())


# ---------------------------------------------------------------------------
# Debian 13 nesting + template selection (retained/updated from the
# original suite)
# ---------------------------------------------------------------------------


class TestContainerCreation:
    def test_debian13_nesting_enabled(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(tmp_path, source_checkout)
        assert result.returncode == 0, result.stderr
        state = fake_env_obj.state()
        assert "nesting=1" in state["vmids"]["110"]["features"]

    def test_global_newest_template_selected_across_storages(self, tmp_path, source_checkout):
        older = "local:vztmpl/debian-13-standard_13.5-1_amd64.tar.zst"
        newer_on_other_storage = "nas:vztmpl/debian-13-standard_13.10-1_amd64.tar.zst"
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={
                "storages": {"rootdir": ["local-lxc"], "vztmpl": ["local", "nas"]},
                "local_templates": [older, newer_on_other_storage],
            },
        )
        assert result.returncode == 0, result.stderr
        assert "debian-13-standard_13.10-1_amd64.tar.zst" in result.stderr

    def test_no_debian13_template_anywhere_is_rejected(self, tmp_path, source_checkout):
        result, _ = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={"local_templates": [], "available_templates": []},
        )
        assert result.returncode != 0
