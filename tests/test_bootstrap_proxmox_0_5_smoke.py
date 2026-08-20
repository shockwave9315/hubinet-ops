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
    # Bytes mode throughout, decoded manually below -- NOT
    # subprocess.run(text=True, input=...): on Windows, Python's text-mode
    # stdin pipe wrapper translates a plain "\n" in `input` to the
    # platform line separator ("\r\n") when WRITING to the child process,
    # so a fed answer like "y\n" would actually arrive at bash's `read -r`
    # as "y\r" -- which fails to match a `y|Y|yes|YES` case pattern (a
    # real bug this exact form hit once already). Real Linux CI never has
    # this problem (a POSIX pipe never translates bytes), but this
    # harness must still behave correctly when developed/run locally on
    # Windows.
    encoded_input = input_text.encode("utf-8") if input_text is not None else None
    result = subprocess.run(
        argv,
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        timeout=timeout,
        input=encoded_input,
    )
    result.stdout = result.stdout.decode("utf-8", errors="replace")
    result.stderr = result.stderr.decode("utf-8", errors="replace")
    return result


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
# Storage free-space preflight: `pvesm status` reports Total/Used/Available
# in KiB, not bytes -- realistic scenarios below are always expressed in
# KiB, matching what the production parser actually sees.
# ---------------------------------------------------------------------------


class TestStorageFreeSpace:
    def test_realistic_kib_available_passes(self, tmp_path, source_checkout):
        result, _ = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={"storage_available_kib": 100 * 1024 * 1024},  # 100 GiB
        )
        assert result.returncode == 0, result.stderr

    def test_insufficient_kib_available_fails(self, tmp_path, source_checkout):
        # 7 GiB available, 8 GiB (default --rootfs-size) requested.
        result, _ = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={"storage_available_kib": 7 * 1024 * 1024},
        )
        assert result.returncode != 0
        assert "does not report enough free space" in result.stderr

    def test_exactly_enough_kib_available_passes(self, tmp_path, source_checkout):
        result, _ = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={"storage_available_kib": 8 * 1024 * 1024},
        )
        assert result.returncode == 0, result.stderr

    def test_malformed_available_value_fails_closed(self, tmp_path, source_checkout):
        # A prior version of this check logged a warning and silently
        # SKIPPED (returned success) on an unparseable Available column --
        # that is exactly the false-pass this preflight gate must never
        # produce. An unparseable capacity value must stop the run.
        result, _ = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={"storage_available_kib": None, "storage_available_raw": "N/A"},
        )
        assert result.returncode != 0
        assert "could not reliably parse available free space" in result.stderr

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
# UX Hardening 6: exactly one interactive confirmation, not two. The
# detected source SHA is validated and surfaced in the single upfront
# plan; a separate "Deploy exactly this commit?" prompt no longer exists.
# ---------------------------------------------------------------------------


class TestSingleConfirmationUx:
    def test_interactive_mode_without_expected_sha_asks_exactly_one_confirmation(self, tmp_path, source_checkout):
        # If a second, separate source-commit prompt still existed, a
        # single "y\n" would only satisfy the FIRST prompt, the second
        # `read` would hit EOF (empty reply -> treated as "no"), and the
        # run would abort. Succeeding on exactly one "y\n" proves only one
        # prompt exists.
        fake_env_obj = build_fake_pve_environment(tmp_path, default_scenario())
        args = _base_args(source_checkout, **{"--non-interactive": False, "--yes": False, "--expected-sha": False})
        result = _run(fake_env_obj.env, args, source_dir=source_checkout, input_text="y\n")
        assert result.returncode == 0, result.stderr
        assert "Detected source commit:" in result.stderr
        assert "Discovery:            PASS" in result.stdout

    def test_interactive_mode_declining_the_single_prompt_aborts_before_any_mutation(self, tmp_path, source_checkout):
        fake_env_obj = build_fake_pve_environment(tmp_path, default_scenario())
        args = _base_args(source_checkout, **{"--non-interactive": False, "--yes": False, "--expected-sha": False})
        result = _run(fake_env_obj.env, args, source_dir=source_checkout, input_text="n\n")
        assert result.returncode != 0
        assert "aborted by operator" in result.stderr
        for line in fake_env_obj.log_lines():
            assert not line.startswith("pct create")
            assert not line.startswith("pveum user add")

    def test_yes_flag_skips_the_prompt_but_still_requires_clean_tree(self, tmp_path, source_checkout):
        # Interactive (--non-interactive: False) + --yes: the ONE
        # remaining plan-confirmation prompt is skipped, but --yes must
        # never make an unconfirmed/dirty source acceptable.
        dirty_file = source_checkout / "requirements.txt"
        original = dirty_file.read_text(encoding="utf-8")
        dirty_file.write_text(original + "# dirty\n", encoding="utf-8")
        try:
            fake_env_obj = build_fake_pve_environment(tmp_path, default_scenario())
            args = _base_args(source_checkout, **{"--non-interactive": False, "--expected-sha": False})
            result = _run(fake_env_obj.env, args, source_dir=source_checkout)
            assert result.returncode != 0
            assert "dirty working tree" in result.stderr
        finally:
            dirty_file.write_text(original, encoding="utf-8")

    def test_yes_flag_never_accepts_a_mismatched_expected_sha(self, tmp_path, source_checkout):
        fake_env_obj = build_fake_pve_environment(tmp_path, default_scenario())
        args = _base_args(source_checkout, **{"--expected-sha": "0" * 40})
        result = _run(fake_env_obj.env, args, source_dir=source_checkout)
        assert result.returncode != 0
        assert "does not match --expected-sha" in result.stderr

    def test_non_interactive_still_unconditionally_requires_expected_sha(self, tmp_path, source_checkout):
        fake_env_obj = build_fake_pve_environment(tmp_path, default_scenario())
        args = _base_args(source_checkout, **{"--expected-sha": False})  # --non-interactive/--yes stay in base args
        result = _run(fake_env_obj.env, args, source_dir=source_checkout)
        assert result.returncode != 0
        assert "--non-interactive requires --expected-sha" in result.stderr


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
        state["pve_users"]["hubinetops@pve"] = {"comment": "pre-existing operator user"}
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
# PVE identity ownership (Blocker 3): rollback must delete only what THIS
# run can PROVE it owns, never merely "whatever exists at our fixed name."
# Uses the fake's `ambiguous_pveum_ops` scenario key: "self" simulates
# this run's own mutating call silently succeeding server-side despite
# reporting failure to us; a literal string simulates a foreign/concurrent
# creator having already populated the object under a DIFFERENT comment.
# ---------------------------------------------------------------------------


class TestPveIdentityOwnership:
    _FOREIGN_COMMENT = (
        "Hubinet Ops 0.5 R0 read-only discovery "
        "(created by bootstrap-proxmox-0.5.sh; run=SOME-OTHER-CONCURRENT-RUN-ID)"
    )

    def test_concurrent_owner_race_user_add_never_deletes_the_winners_user(self, tmp_path, source_checkout):
        # The exact scenario from the mission: A and B both observe the
        # user absent; A creates it first; B's own `pveum user add` fails
        # because it now exists (simulated here directly via the fake
        # rather than real concurrency -- B never gets to see a comment
        # carrying B's own BOOTSTRAP_RUN_ID, only A's foreign one).
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={"ambiguous_pveum_ops": {"user_add": self._FOREIGN_COMMENT}},
        )
        assert result.returncode != 0
        assert not any(line.startswith("pveum user delete") for line in fake_env_obj.log_lines())
        assert "PRESERVING PVE user" in result.stderr
        state = fake_env_obj.state()
        assert "hubinetops@pve" in state["pve_users"]
        assert state["pve_users"]["hubinetops@pve"]["comment"] == self._FOREIGN_COMMENT

    def test_concurrent_owner_race_never_touches_role_token_or_acl_either(self, tmp_path, source_checkout):
        # B dies at the user-add step (never reaches role/token/ACL
        # creation at all) -- confirms nothing downstream of the race is
        # even attempted, let alone deleted.
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={"ambiguous_pveum_ops": {"user_add": self._FOREIGN_COMMENT}},
        )
        assert result.returncode != 0
        log = fake_env_obj.log_lines()
        assert not any(line.startswith("pveum role add") for line in log)
        assert not any(line.startswith("pveum role delete") for line in log)
        assert not any("token" in line for line in log if line.startswith("pveum"))

    def test_ambiguous_self_success_user_is_proven_and_cleaned_up(self, tmp_path, source_checkout):
        # This run's OWN user-add call actually succeeded server-side
        # (the fake stores the real argv comment, which carries this
        # run's real BOOTSTRAP_RUN_ID) despite reporting failure --
        # rollback must be able to prove ownership from that comment and
        # clean it up rather than leaving an orphan behind.
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={"ambiguous_pveum_ops": {"user_add": "self"}},
        )
        assert result.returncode != 0
        assert any(line.startswith("pveum user delete hubinetops@pve") for line in fake_env_obj.log_lines())
        state = fake_env_obj.state()
        assert "hubinetops@pve" not in state["pve_users"]

    def test_ambiguous_role_creation_is_never_blindly_deleted(self, tmp_path, source_checkout):
        # PVE roles carry no comment/provenance field -- an ambiguous
        # role-add failure can NEVER be proven either way and must always
        # be preserved, regardless of whether it was actually this run's
        # own doing or a concurrent creator's.
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={"ambiguous_pveum_ops": {"role_add": "self"}},
        )
        assert result.returncode != 0
        assert not any(line.startswith("pveum role delete") for line in fake_env_obj.log_lines())
        assert "PRESERVING PVE role" in result.stderr
        state = fake_env_obj.state()
        assert "HubinetOpsR0Auditor" in state["pve_roles"]

    def test_ambiguous_role_creation_preserves_manual_remediation_message(self, tmp_path, source_checkout):
        result, _ = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={"ambiguous_pveum_ops": {"role_add": "self"}},
        )
        assert result.returncode != 0
        assert "pveum role delete HubinetOpsR0Auditor" in result.stderr

    def test_ambiguous_token_creation_under_an_owned_user_is_never_blindly_deleted(self, tmp_path, source_checkout):
        # The user is genuinely ours (clean success); the token-add call
        # is ambiguous and cannot be attributed to this run (no matching
        # run-id comment) -- the token must be preserved even though its
        # parent user is legitimately cleaned up.
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={"ambiguous_pveum_ops": {"token_add": self._FOREIGN_COMMENT}},
        )
        assert result.returncode != 0
        log = fake_env_obj.log_lines()
        assert not any(line.startswith("pveum user token remove") for line in log)
        assert "PRESERVING PVE token" in result.stderr
        # The user itself is still legitimately owned/cleaned up.
        assert any(line.startswith("pveum user delete hubinetops@pve") for line in log)

    def test_ambiguous_token_self_success_is_proven_and_cleaned_up(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={"ambiguous_pveum_ops": {"token_add": "self"}},
        )
        assert result.returncode != 0
        assert any(
            line.startswith("pveum user token remove hubinetops@pve r0-readonly")
            for line in fake_env_obj.log_lines()
        )

    def test_run_id_is_embedded_in_user_and_token_comments_on_success(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(tmp_path, source_checkout)
        assert result.returncode == 0, result.stderr
        state = fake_env_obj.state()
        user_comment = state["pve_users"]["hubinetops@pve"]["comment"]
        token_comment = state["pve_tokens"]["hubinetops@pve!r0-readonly"]["comment"]
        assert "run=" in user_comment
        assert "run=" in token_comment
        # Same run-id in both (one bootstrap invocation, one run-id).
        user_run_id = user_comment.split("run=", 1)[1].rstrip(")")
        token_run_id = token_comment.split("run=", 1)[1].rstrip(")")
        assert user_run_id == token_run_id
        assert len(user_run_id) >= 8  # not a trivially-guessable/empty marker


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
            scenario_overrides={"dns_resolution": {"pve.example.invalid": ["203.0.113.10"]}},
        )
        assert result.returncode == 0, result.stderr
        ruleset = fake_env_obj.ct_file_text("110", "/etc/nftables.conf")
        assert 'ip daddr 198.51.100.53 udp dport 53 accept' in ruleset
        assert 'ip daddr 198.51.100.53 tcp dport 53 accept' in ruleset

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
# Hostname PVE endpoint firewall resolution (Blocker 4): nftables reports
# resolved numeric addresses in `nft list ruleset`, never the original
# hostname text -- the bootstrap must resolve the hostname to concrete
# IPv4 addresses INSIDE the CT before generating the ruleset, and verify
# against those literal numeric addresses, never the hostname itself.
# ---------------------------------------------------------------------------


class TestHostnameFirewallResolution:
    _HOSTNAME_ENDPOINT = "https://pve.example.invalid:8006"

    def test_single_a_record_resolves_to_one_exact_rule(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            args=["--pve-endpoint", self._HOSTNAME_ENDPOINT, "--dns-resolver", "198.51.100.53"],
            scenario_overrides={"dns_resolution": {"pve.example.invalid": ["203.0.113.10"]}},
        )
        assert result.returncode == 0, result.stderr
        ruleset = fake_env_obj.ct_file_text("110", "/etc/nftables.conf")
        assert 'meta skuid "hubinetops" ip daddr 203.0.113.10 tcp dport 8006 accept' in ruleset
        # Never the literal hostname text anywhere in the generated rules.
        assert "pve.example.invalid" not in ruleset

    def test_multiple_a_records_each_get_their_own_exact_rule(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            args=["--pve-endpoint", self._HOSTNAME_ENDPOINT, "--dns-resolver", "198.51.100.53"],
            scenario_overrides={
                "dns_resolution": {"pve.example.invalid": ["203.0.113.10", "203.0.113.11"]},
            },
        )
        assert result.returncode == 0, result.stderr
        ruleset = fake_env_obj.ct_file_text("110", "/etc/nftables.conf")
        assert 'meta skuid "hubinetops" ip daddr 203.0.113.10 tcp dport 8006 accept' in ruleset
        assert 'meta skuid "hubinetops" ip daddr 203.0.113.11 tcp dport 8006 accept' in ruleset

    def test_duplicate_a_records_are_deduplicated(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            args=["--pve-endpoint", self._HOSTNAME_ENDPOINT, "--dns-resolver", "198.51.100.53"],
            scenario_overrides={
                "dns_resolution": {"pve.example.invalid": ["203.0.113.10", "203.0.113.10"]},
            },
        )
        assert result.returncode == 0, result.stderr
        ruleset = fake_env_obj.ct_file_text("110", "/etc/nftables.conf")
        assert ruleset.count("ip daddr 203.0.113.10 tcp dport 8006 accept") == 1

    def test_zero_a_records_is_a_hard_stop(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            args=["--pve-endpoint", self._HOSTNAME_ENDPOINT, "--dns-resolver", "198.51.100.53"],
            scenario_overrides={"dns_resolution": {"pve.example.invalid": []}},
        )
        assert result.returncode != 0
        assert not any(line.startswith("pct push") and "nftables.conf" in line for line in fake_env_obj.log_lines())

    def test_resolution_failure_is_a_hard_stop(self, tmp_path, source_checkout):
        # Host not present in the fake's dns_resolution mapping at all --
        # simulates a genuine resolution failure (NXDOMAIN/unreachable
        # resolver).
        result, _ = _run_full(
            tmp_path, source_checkout,
            args=["--pve-endpoint", self._HOSTNAME_ENDPOINT, "--dns-resolver", "198.51.100.53"],
        )
        assert result.returncode != 0
        assert "could not resolve PVE endpoint hostname" in result.stderr

    def test_resolver_gets_exact_udp_and_tcp_53_rules(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            args=["--pve-endpoint", self._HOSTNAME_ENDPOINT, "--dns-resolver", "198.51.100.53"],
            scenario_overrides={"dns_resolution": {"pve.example.invalid": ["203.0.113.10"]}},
        )
        assert result.returncode == 0, result.stderr
        ruleset = fake_env_obj.ct_file_text("110", "/etc/nftables.conf")
        assert 'meta skuid "hubinetops" ip daddr 198.51.100.53 udp dport 53 accept' in ruleset
        assert 'meta skuid "hubinetops" ip daddr 198.51.100.53 tcp dport 53 accept' in ruleset

    def test_literal_ip_endpoint_gets_no_dns_rule_and_no_resolution_call(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(tmp_path, source_checkout)  # default endpoint is a literal IP
        assert result.returncode == 0, result.stderr
        ruleset = fake_env_obj.ct_file_text("110", "/etc/nftables.conf")
        assert "udp dport 53" not in ruleset
        assert "tcp dport 53" not in ruleset
        assert not any(
            "hubinet-ops-bootstrap-resolve-dns.py" in line for line in fake_env_obj.log_lines()
        )

    def test_exact_numeric_readback_verifies_even_though_endpoint_is_a_hostname(self, tmp_path, source_checkout):
        # The whole point: exact-content/order firewall verification
        # (phase 10's own recheck, plus phase 12's later recheck) must
        # succeed against the resolved numeric address, never fail
        # closed merely because the configured endpoint was a hostname.
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            args=["--pve-endpoint", self._HOSTNAME_ENDPOINT, "--dns-resolver", "198.51.100.53"],
            scenario_overrides={"dns_resolution": {"pve.example.invalid": ["203.0.113.10"]}},
        )
        assert result.returncode == 0, result.stderr
        assert "Discovery:            PASS" in result.stdout
        state = fake_env_obj.state()
        assert state["vmids"]["110"]["onboot"] == "1"

    def test_tls_hostname_verification_is_unaffected_endpoint_stays_the_hostname(self, tmp_path, source_checkout):
        # The firewall permits the resolved numeric IP(s), but the R0
        # runtime's OWN configured pve_endpoint (used for TLS certificate
        # hostname verification) must remain the original hostname --
        # never rewritten to the resolved IP.
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            args=["--pve-endpoint", self._HOSTNAME_ENDPOINT, "--dns-resolver", "198.51.100.53"],
            scenario_overrides={"dns_resolution": {"pve.example.invalid": ["203.0.113.10"]}},
        )
        assert result.returncode == 0, result.stderr
        inventory = fake_env_obj.ct_file_text("110", "/etc/hubinet-ops/inventory.yaml")
        assert f'pve_endpoint: "{self._HOSTNAME_ENDPOINT}"' in inventory


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

    # -- Hardening 5: PASS proves a genuinely committed, fresh, current
    #    discovery success -- not merely health == "healthy" in isolation.

    def test_healthy_but_stale_never_passes(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={"discovery_result": "healthy_but_stale"},
        )
        assert result.returncode != 0
        state = fake_env_obj.state()
        assert state["vmids"]["110"]["onboot"] == "0"

    def test_unsuccessful_completed_outcome_never_passes(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={"discovery_result": "unsuccessful_outcome"},
        )
        assert result.returncode != 0
        assert "latest-completed-outcome-not-success" in result.stderr
        state = fake_env_obj.state()
        assert state["vmids"]["110"]["onboot"] == "0"

    def test_missing_committed_context_never_passes(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={"discovery_result": "missing_committed_context"},
        )
        assert result.returncode != 0
        assert "committed-context-missing" in result.stderr
        state = fake_env_obj.state()
        assert state["vmids"]["110"]["onboot"] == "0"

    def test_committed_current_context_mismatch_never_passes(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={"discovery_result": "context_mismatch"},
        )
        assert result.returncode != 0
        assert "committed-current-context-mismatch" in result.stderr
        state = fake_env_obj.state()
        assert state["vmids"]["110"]["onboot"] == "0"

    def test_zero_nodes_after_healthy_commit_never_passes(self, tmp_path, source_checkout):
        # A real PVE source necessarily has at least its own node --
        # empty nodes[] after an otherwise-healthy commit is suspicious
        # and must never produce the final PASS.
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={"discovery_result": "zero_nodes"},
        )
        assert result.returncode != 0
        assert "zero-nodes-after-healthy-commit" in result.stderr
        state = fake_env_obj.state()
        assert state["vmids"]["110"]["onboot"] == "0"

    def test_zero_resources_with_one_real_node_and_valid_committed_source_passes(self, tmp_path, source_checkout):
        # The positive control for the two checks above: zero resources
        # is legitimate (already covered by
        # test_zero_resources_on_healthy_source_is_legitimate), but this
        # pins it specifically alongside a real, non-empty nodes[] and a
        # fully valid committed/current context (the default simulated
        # scenario), i.e. exactly the PASS shape Hardening 5 requires.
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={"discovery_resource_count": 0, "discovery_node_count": 1},
        )
        assert result.returncode == 0, result.stderr
        assert "Discovery:            PASS" in result.stdout
        state = fake_env_obj.state()
        assert state["vmids"]["110"]["onboot"] == "1"

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
