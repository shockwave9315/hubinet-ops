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
    FAKE_HA_SOURCE_CANONICAL,
    FAKE_HA_SOURCE_CIDR,
    FAKE_HUBINETOPS_UID,
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
# Real-PVE corrective fix: `pveum user token permissions <user> <token>
# --path / --output-format json` was assumed to return a flat object of
# privilege names directly at the top level. A real-host read-only
# precheck against Proxmox VE 9.2.3 disproved that -- the real command
# returns a PATH-KEYED object instead ({"/": {"Sys.Audit": 1, ...}}),
# observed literally as `{"/":{}}` for an empty grant (see
# docs/architecture/0.5-implementation-status.md's real-PVE precheck
# notes). These tests exercise _verify_effective_permissions' full
# exact-set STOP/proceed outcome end to end via the fake's
# "pveum_output_override": {"token_permissions": ...} hook (which now
# emits whatever raw JSON text a test supplies, in place of the fake's
# own normally-computed path-keyed response) -- including explicitly
# proving the OLD flat-object shape this bootstrap used to (wrongly)
# assume is now rejected rather than silently misread as "zero
# privileges."
# ---------------------------------------------------------------------------


class TestPathKeyedEffectivePermissions:
    def _run_with_permissions_json(self, tmp_path, source_checkout, raw_json):
        return _run_full(
            tmp_path, source_checkout,
            scenario_overrides={"pveum_output_override": {"token_permissions": raw_json}},
        )

    @pytest.mark.parametrize(
        "raw_json",
        ['{"/":{"Sys.Audit":1,"VM.Audit":1}}', '{"/":{"VM.Audit":1,"Sys.Audit":1}}'],
    )
    def test_exact_path_keyed_grant_passes_regardless_of_key_order(self, tmp_path, source_checkout, raw_json):
        result, _ = self._run_with_permissions_json(tmp_path, source_checkout, raw_json)
        assert result.returncode == 0, result.stderr
        assert "PASS: PVE identity" in result.stderr

    def test_missing_required_privilege_stops(self, tmp_path, source_checkout):
        result, _ = self._run_with_permissions_json(tmp_path, source_checkout, '{"/":{"Sys.Audit":1}}')
        assert result.returncode != 0
        assert "verification failed" in result.stderr

    def test_unexpected_extra_privilege_stops(self, tmp_path, source_checkout):
        result, _ = self._run_with_permissions_json(
            tmp_path, source_checkout, '{"/":{"Sys.Audit":1,"VM.Audit":1,"VM.PowerMgmt":1}}',
        )
        assert result.returncode != 0
        assert "verification failed" in result.stderr
        assert "VM.PowerMgmt" in result.stderr

    def test_requested_path_missing_stops(self, tmp_path, source_checkout):
        result, _ = self._run_with_permissions_json(
            tmp_path, source_checkout, '{"/vms":{"Sys.Audit":1,"VM.Audit":1}}',
        )
        assert result.returncode != 0
        assert "did not produce the expected path-keyed JSON shape" in result.stderr

    def test_wrong_root_shape_stops(self, tmp_path, source_checkout):
        result, _ = self._run_with_permissions_json(tmp_path, source_checkout, "[]")
        assert result.returncode != 0
        assert "did not produce a valid JSON object" in result.stderr

    def test_slash_wrong_shape_stops(self, tmp_path, source_checkout):
        result, _ = self._run_with_permissions_json(tmp_path, source_checkout, '{"/":[]}')
        assert result.returncode != 0
        assert "did not produce the expected path-keyed JSON shape" in result.stderr

    def test_old_flat_object_shape_now_explicitly_rejected(self, tmp_path, source_checkout):
        # This is the exact shape the fake/production code WRONGLY
        # assumed before the real-PVE-9.2.3 precheck -- proving it can
        # never silently regress back to the invented flat contract
        # (which would otherwise be misread as "zero privileges granted
        # at /" -- still fail-closed by accident, but for the wrong
        # reason and with a confusing message).
        result, _ = self._run_with_permissions_json(
            tmp_path, source_checkout, '{"Sys.Audit":1,"VM.Audit":1}',
        )
        assert result.returncode != 0
        assert "did not produce the expected path-keyed JSON shape" in result.stderr

    def test_command_failure_stops(self, tmp_path, source_checkout):
        result, _ = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={"fail": ["pveum_token_permissions"]},
        )
        assert result.returncode != 0
        assert "could not read back effective permissions" in result.stderr

    def test_malformed_json_stops(self, tmp_path, source_checkout):
        result, _ = self._run_with_permissions_json(tmp_path, source_checkout, "not-valid-json{{{")
        assert result.returncode != 0
        assert "did not produce a valid JSON object" in result.stderr

    def test_real_observed_empty_grant_is_schema_valid_but_still_fails_exact_set(self, tmp_path, source_checkout):
        # The literal real-PVE-9.2.3 observation {"/":{}} is schema-VALID
        # (a genuine path-keyed object, just with nothing granted yet) --
        # it must reach the exact-set comparison (and fail there, on a
        # correctness basis: zero privileges != {Sys.Audit, VM.Audit}),
        # never be rejected as an unrecognized shape.
        result, _ = self._run_with_permissions_json(tmp_path, source_checkout, '{"/":{}}')
        assert result.returncode != 0
        assert "verification failed" in result.stderr
        assert "did not produce" not in result.stderr


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
        # User cleanup is still attempted -- ownership can be re-verified
        # live (comment carries this run's id). Role cleanup is NOT (P2-3,
        # third pass): PVE roles have no comment/provenance field, so
        # rollback can never re-verify at delete-time that the CURRENT
        # role is still the one this run created -- it is always
        # preserved, regardless of how "clean" this run's own ledger looks.
        assert any(line.startswith("pveum user delete") for line in log)
        assert not any(line.startswith("pveum role delete") for line in log)
        assert "PRESERVING PVE role" in result.stderr

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
        # run-id comment) -- the token must be preserved.
        #
        # Eighth-pass corrective note (P1 finding, independent review):
        # an earlier version of this test asserted the PARENT USER was
        # "still legitimately cleaned up" here -- but real Proxmox's
        # `pveum user delete` removes the deleted user's ENTIRE
        # configuration, including every token under it, so deleting the
        # user would have indirectly destroyed the very token this same
        # test proves is preserved. The parent user must ALSO be
        # preserved whenever its token's ownership is unproven.
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={"ambiguous_pveum_ops": {"token_add": self._FOREIGN_COMMENT}},
        )
        assert result.returncode != 0
        log = fake_env_obj.log_lines()
        assert not any(line.startswith("pveum user token remove") for line in log)
        assert "PRESERVING PVE token" in result.stderr
        # The parent user must NOT be deleted either -- doing so would
        # destroy the unproven token as a side effect.
        assert not any(line.startswith("pveum user delete") for line in log)
        assert "PRESERVING PVE user" in result.stderr
        state = fake_env_obj.state()
        assert "hubinetops@pve" in state["pve_users"]
        assert "hubinetops@pve!r0-readonly" in state["pve_tokens"]

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

    # -----------------------------------------------------------------
    # P2-3, third pass: rollback ownership must ALWAYS be a live
    # read-back of the CURRENT object, never authorized by this run's own
    # ledger alone. "replace_identity_before_failure" simulates an
    # external actor deleting and recreating the fixed-name object
    # sometime between this run's own successful creation (ledger
    # recorded) and a later, unrelated phase failing and triggering
    # rollback -- apt-get (tooling provisioning) is the later failure
    # point used throughout.
    # -----------------------------------------------------------------

    def test_ledger_success_but_replaced_live_user_before_rollback_survives(self, tmp_path, source_checkout):
        foreign = "replaced by another admin; run=SOME-OTHER-ACTOR"
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={
                "fail": ["apt_get"],
                "replace_identity_before_failure": {"apt_get": {"user_comment": foreign}},
            },
        )
        assert result.returncode != 0
        log = fake_env_obj.log_lines()
        assert not any(line.startswith("pveum user delete") for line in log)
        assert "PRESERVING PVE user" in result.stderr
        state = fake_env_obj.state()
        assert state["pve_users"]["hubinetops@pve"]["comment"] == foreign

    def test_ledger_success_but_replaced_live_token_before_rollback_survives(self, tmp_path, source_checkout):
        # Eighth-pass corrective note (P1 finding, independent review):
        # even though the USER object itself was not replaced (still
        # provably ours), its TOKEN was -- and an unproven token must
        # block automatic deletion of its parent user too, since
        # `pveum user delete` would otherwise destroy that unproven token
        # as a side effect of removing the user it lives under.
        foreign = "replaced by another admin; run=SOME-OTHER-ACTOR"
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={
                "fail": ["apt_get"],
                "replace_identity_before_failure": {"apt_get": {"token_comment": foreign}},
            },
        )
        assert result.returncode != 0
        log = fake_env_obj.log_lines()
        assert not any(line.startswith("pveum user token remove") for line in log)
        assert "PRESERVING PVE token" in result.stderr
        state = fake_env_obj.state()
        assert state["pve_tokens"]["hubinetops@pve!r0-readonly"]["comment"] == foreign
        # The parent user must be preserved too, despite being provably
        # ours -- deleting it would destroy the unproven token.
        assert not any(line.startswith("pveum user delete") for line in log)
        assert "PRESERVING PVE user" in result.stderr
        assert "hubinetops@pve" in state["pve_users"]

    def test_current_user_with_correct_run_id_may_still_be_removed(self, tmp_path, source_checkout):
        # Positive control: an ordinary clean creation followed by an
        # unrelated later-phase failure (no replacement at all) must still
        # result in the user being cleaned up -- the always-live-read-back
        # change must not break the ordinary rollback path.
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout, scenario_overrides={"fail": ["apt_get"]}
        )
        assert result.returncode != 0
        assert any(
            line.startswith("pveum user delete hubinetops@pve") for line in fake_env_obj.log_lines()
        )

    def test_current_token_with_correct_run_id_may_still_be_removed(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout, scenario_overrides={"fail": ["apt_get"]}
        )
        assert result.returncode != 0
        assert any(
            line.startswith("pveum user token remove hubinetops@pve r0-readonly")
            for line in fake_env_obj.log_lines()
        )

    def test_token_genuinely_absent_still_allows_owned_user_deletion(self, tmp_path, source_checkout):
        # Tri-state positive control (eighth pass, P1 finding): a
        # schema-valid read-back that simply does not contain the token
        # at all is the ONE "not owned" outcome that is safe to treat as
        # "nothing to protect" -- the owned parent user may still be
        # deleted normally. This must not be conflated with "unproven."
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={
                "fail": ["apt_get"],
                "pveum_output_override": {"token_list": "[]"},
            },
        )
        assert result.returncode != 0
        log = fake_env_obj.log_lines()
        # Genuinely absent -- never logged as "preserved" (there is
        # nothing to preserve), and never attempted to be removed either.
        assert "PRESERVING PVE token" not in result.stderr
        assert not any(line.startswith("pveum user token remove") for line in log)
        # The owned user is still legitimately deleted.
        assert any(line.startswith("pveum user delete hubinetops@pve") for line in log)
        assert "PRESERVING PVE user" not in result.stderr
        # The user's own ACL grant is cleaned up normally too -- confirms
        # the "absent" branch is not being conservative for the wrong
        # reason (e.g. an accidental blanket ACL-skip).
        assert any(line.startswith("pveum acl delete / --users hubinetops@pve") for line in log)

    # -----------------------------------------------------------------
    # Ninth-pass corrective fix, P1/P2 (independent review): an unproven
    # token must block mutation of the WHOLE identity dependency chain
    # (token, token ACL, parent user, parent user ACL) -- not merely the
    # token object and the parent user object, leaving their ACL grants
    # exposed to removal in between.
    # -----------------------------------------------------------------

    def test_unproven_token_preserves_both_token_acl_and_user_acl(self, tmp_path, source_checkout):
        # A privsep=1 token's effective permissions are the INTERSECTION
        # of the owning user's permissions and the token's own -- so even
        # removing only the PARENT USER's own ACL grant (while leaving
        # both the user and token objects themselves untouched) can
        # functionally disable an unproven/possibly-foreign token.
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={
                "fail": ["apt_get"],
                "pveum_output_override": {"token_list": '["hubinetops@pve!r0-readonly"]'},
            },
        )
        assert result.returncode != 0
        log = fake_env_obj.log_lines()
        # Nothing in the whole dependency chain is mutated.
        assert not any(line.startswith("pveum acl delete") for line in log)
        assert not any(line.startswith("pveum user token remove") for line in log)
        assert not any(line.startswith("pveum user delete") for line in log)
        assert "PRESERVING PVE token" in result.stderr
        assert "PRESERVING PVE user" in result.stderr
        state = fake_env_obj.state()
        acl_targets = {grant["target"] for grant in state["acl_grants"]}
        assert "user:hubinetops@pve" in acl_targets
        assert "token:hubinetops@pve!r0-readonly" in acl_targets
        assert "hubinetops@pve" in state["pve_users"]
        assert "hubinetops@pve!r0-readonly" in state["pve_tokens"]

    def test_owned_token_removal_command_failure_preserves_user_and_user_acl(self, tmp_path, source_checkout):
        # P2 finding (independent review): the token is genuinely proven
        # OWNED (schema-valid read, comment carries this run's marker),
        # but the removal command itself fails -- parent_user_cleanup_
        # safe must NOT become true merely because ownership was proven;
        # it requires the removal command to have actually succeeded.
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={"fail": ["apt_get", "pveum_user_token_remove"]},
        )
        assert result.returncode != 0
        log = fake_env_obj.log_lines()
        # The token's own ACL grant IS attempted (independent step) --
        # only the subsequent removal command fails.
        assert any(line.startswith("pveum acl delete / --tokens") for line in log)
        assert any(line.startswith("pveum user token remove") for line in log)
        assert "could not remove token" in result.stderr
        assert "token cleanup is INCOMPLETE" in result.stderr
        # The parent user and its OWN acl grant must both be preserved.
        assert not any(line.startswith("pveum acl delete / --users") for line in log)
        assert not any(line.startswith("pveum user delete") for line in log)
        assert "PRESERVING PVE user" in result.stderr
        state = fake_env_obj.state()
        acl_targets = {grant["target"] for grant in state["acl_grants"]}
        assert "user:hubinetops@pve" in acl_targets
        assert "hubinetops@pve" in state["pve_users"]
        # The token object itself DID get removed from PVE's state by
        # the fake's own "remove" handler failing before mutating state
        # -- i.e. the fake never actually pops it when it reports
        # failure -- confirming the token, too, is genuinely still there.
        assert "hubinetops@pve!r0-readonly" in state["pve_tokens"]

    def test_user_ownership_readback_command_failure_preserves(self, tmp_path, source_checkout):
        # Call #1 (phase6's own pre-existing-conflict check) succeeds;
        # call #2 (rollback's live read-back) fails.
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={"fail": ["apt_get"], "pveum_user_list_fail_after_calls": 1},
        )
        assert result.returncode != 0
        assert not any(line.startswith("pveum user delete") for line in fake_env_obj.log_lines())
        assert "PRESERVING PVE user" in result.stderr

    def test_user_ownership_readback_malformed_json_preserves(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={"fail": ["apt_get"], "pveum_user_list_malformed_after_calls": 1},
        )
        assert result.returncode != 0
        assert not any(line.startswith("pveum user delete") for line in fake_env_obj.log_lines())
        assert "PRESERVING PVE user" in result.stderr

    def test_token_ownership_readback_command_failure_preserves(self, tmp_path, source_checkout):
        # Eighth-pass corrective note (P1 finding, independent review): a
        # token read-back COMMAND FAILURE is UNPROVEN, not "safe to treat
        # the user's ownership check as independent" -- an unproven token
        # must block automatic deletion of its parent user too.
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={"fail": ["apt_get", "pveum_token_list"]},
        )
        assert result.returncode != 0
        log = fake_env_obj.log_lines()
        assert not any(line.startswith("pveum user token remove") for line in log)
        assert "PRESERVING PVE token" in result.stderr
        # The parent user must be preserved too -- deleting it would
        # destroy the unproven token as a side effect.
        assert not any(line.startswith("pveum user delete") for line in log)
        assert "PRESERVING PVE user" in result.stderr

    def test_token_ownership_readback_malformed_json_preserves(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={
                "fail": ["apt_get"],
                "malformed_pveum_output": {"token_list": True},
            },
        )
        assert result.returncode != 0
        log = fake_env_obj.log_lines()
        assert not any(line.startswith("pveum user token remove") for line in log)
        assert "PRESERVING PVE token" in result.stderr
        # Eighth-pass corrective note: malformed token JSON is UNPROVEN,
        # which must also block automatic deletion of the parent user.
        assert not any(line.startswith("pveum user delete") for line in log)
        assert "PRESERVING PVE user" in result.stderr

    def test_role_rollback_always_conservative_even_on_a_fully_clean_run(self, tmp_path, source_checkout):
        # PVE roles have no comment/provenance field at all -- even in a
        # totally unambiguous, cleanly-ledgered rollback, the role can
        # never be re-verified live, so it must always be preserved.
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout, scenario_overrides={"fail": ["apt_get"]}
        )
        assert result.returncode != 0
        assert not any(line.startswith("pveum role delete") for line in fake_env_obj.log_lines())
        assert "PRESERVING PVE role" in result.stderr
        state = fake_env_obj.state()
        assert "HubinetOpsR0Auditor" in state["pve_roles"]

    # -----------------------------------------------------------------
    # Schema validation (fourth pass): an unexpected-but-syntactically-
    # valid JSON shape on the ROLLBACK read-back must mean ownership is
    # UNPROVEN -- preserve, never a false "owned" that could delete an
    # object this run cannot actually verify is its own.
    # -----------------------------------------------------------------

    def test_user_ownership_readback_schema_invalid_preserves(self, tmp_path, source_checkout):
        # Call #1 (phase6's own pre-existing-conflict check) sees the
        # normal, well-formed listing; call #2 (rollback's live read-back)
        # sees a schema-invalid one -- must never be read as a false match
        # for BOOTSTRAP_RUN_ID, and must never be silently treated as
        # "absent" either (both are simply "unproven").
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={
                "fail": ["apt_get"],
                "pveum_output_override": {"user_list": '[{"user":"hubinetops@pve"}]'},
                "pveum_user_list_override_after_calls": 1,
            },
        )
        assert result.returncode != 0
        assert not any(line.startswith("pveum user delete") for line in fake_env_obj.log_lines())
        assert "PRESERVING PVE user" in result.stderr
        # Sixth-pass corrective note: this scenario is exactly the real
        # witness -- a clean ledger success (phase6 succeeded; apt_get
        # fails only afterward) followed by a schema-invalid live
        # read-back. The specific structural diagnosis must be present.
        # Seventh-pass corrective note: `{"user": ...}` is an object
        # missing the required "userid" field -- refined diagnosis is now
        # the more precise "required-field-missing", not the old coarse
        # catch-all.
        assert "diagnosis: required-field-missing" in result.stderr
        # ...and the generic preserve message must NOT assert a false
        # "no ledger record" claim -- this run's ledger genuinely DID
        # record a clean success.
        assert "no ledger success record" not in result.stderr
        assert "this run could not prove live ownership of the current object" in result.stderr

    def test_token_ownership_readback_schema_invalid_preserves(self, tmp_path, source_checkout):
        # `pveum user token list` is only ever called from the rollback
        # read-back (no earlier successful-path code calls it), so no
        # call-count gating is needed here.
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={
                "fail": ["apt_get"],
                "pveum_output_override": {"token_list": '["hubinetops@pve!r0-readonly"]'},
            },
        )
        assert result.returncode != 0
        log = fake_env_obj.log_lines()
        assert not any(line.startswith("pveum user token remove") for line in log)
        assert "PRESERVING PVE token" in result.stderr
        # Seventh-pass corrective note: a bare-string array element (no
        # object at all) is now diagnosed as the more precise
        # "element-not-object", not the old coarse catch-all.
        assert "diagnosis: element-not-object" in result.stderr
        assert "no ledger success record" not in result.stderr
        assert "this run could not prove live ownership of the current object" in result.stderr
        # Eighth-pass corrective note (P1 finding, independent review): a
        # schema-invalid token read-back is UNPROVEN, which must also
        # block automatic deletion of the parent user -- 'pveum user
        # delete' would otherwise destroy this same unproven token as a
        # side effect of removing its parent.
        assert not any(line.startswith("pveum user delete") for line in log)
        assert "PRESERVING PVE user" in result.stderr

    # -----------------------------------------------------------------
    # Real dogfood #2, Finding B: the diagnostic-only re-read
    # (bootstrap-common.sh::_diagnostic_ownership_reread) can NEVER
    # authorize ownership/deletion, no matter what the second read finds
    # -- the authoritative first read already failed, and that decision
    # is final. These reproduce the real dogfood #2 shape exactly: call
    # #1 (phase6's own pre-existing-conflict check) succeeds normally;
    # call #2 (rollback's authoritative read) is schema-invalid; call #3
    # (the new diagnostic-only re-read) varies per test.
    # -----------------------------------------------------------------

    def test_diagnostic_reread_valid_with_matching_run_marker_still_preserves(
        self, tmp_path, source_checkout,
    ):
        # Call #3 gets NO override at all -- it falls through to the
        # fake's real, current state, which genuinely does carry this
        # run's own comment (the user really was created earlier in this
        # same run) -- exactly the real dogfood #2 shape: a perfectly
        # valid, run-marker-matching second read that must still NOT
        # rescue the object from the already-failed authoritative read.
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={
                "fail": ["apt_get"],
                "pveum_user_list_malformed_at_call": 2,
            },
        )
        assert result.returncode != 0
        assert not any(line.startswith("pveum user delete") for line in fake_env_obj.log_lines())
        assert "PRESERVING PVE user" in result.stderr
        assert "diagnostic reread" in result.stderr
        assert "target_present=true" in result.stderr
        assert "run_marker_match=true" in result.stderr
        # The strong invariant: preserved regardless of the diagnostic
        # reread's own result.
        assert "remains UNPROVEN and PRESERVED" in result.stderr
        state = fake_env_obj.state()
        assert "hubinetops@pve" in state["pve_users"]

    def test_diagnostic_reread_also_malformed_still_preserves(self, tmp_path, source_checkout):
        # Both the authoritative read (call #2) and the diagnostic-only
        # re-read (call #3) are schema-invalid.
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={
                "fail": ["apt_get"],
                "pveum_user_list_malformed_after_calls": 1,
            },
        )
        assert result.returncode != 0
        assert not any(line.startswith("pveum user delete") for line in fake_env_obj.log_lines())
        assert "PRESERVING PVE user" in result.stderr
        assert "diagnostic reread" in result.stderr
        assert "still schema-invalid" in result.stderr
        state = fake_env_obj.state()
        assert "hubinetops@pve" in state["pve_users"]

    def test_diagnostic_reread_command_failure_still_preserves(self, tmp_path, source_checkout):
        # The authoritative read (call #2) is schema-invalid; the
        # diagnostic-only re-read (call #3) fails outright (a second
        # `pveum` command error) rather than merely being malformed again.
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={
                "fail": ["apt_get"],
                "pveum_user_list_malformed_at_call": 2,
                "pveum_user_list_fail_at_call": 3,
            },
        )
        assert result.returncode != 0
        assert not any(line.startswith("pveum user delete") for line in fake_env_obj.log_lines())
        assert "PRESERVING PVE user" in result.stderr
        assert "diagnostic reread" in result.stderr
        assert "also failed (exit 1)" in result.stderr
        state = fake_env_obj.state()
        assert "hubinetops@pve" in state["pve_users"]

    def test_diagnostic_reread_wired_for_token_ownership_too(self, tmp_path, source_checkout):
        # Same conservative pattern applied to the token ownership path
        # (Finding B item 6): the authoritative token-list read (call #1
        # -- token list has no earlier successful-path caller) is
        # schema-invalid; the diagnostic-only re-read (call #2) also
        # fails outright.
        #
        # Eighth-pass corrective note (P1 finding, independent review):
        # an earlier version of this exact test asserted the parent user
        # was STILL deleted here -- cementing the unsafe behavior real
        # Proxmox's 'pveum user delete' would have produced (destroying
        # this same "preserved" token as a side effect of removing its
        # parent user). An unproven token must now also block automatic
        # deletion of its parent user.
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={
                "fail": ["apt_get"],
                "pveum_output_override": {"token_list": '["hubinetops@pve!r0-readonly"]'},
                "pveum_token_list_fail_at_call": 2,
            },
        )
        assert result.returncode != 0
        log = fake_env_obj.log_lines()
        assert not any(line.startswith("pveum user token remove") for line in log)
        assert "PRESERVING PVE token" in result.stderr
        assert "diagnostic reread" in result.stderr
        assert "also failed (exit 1)" in result.stderr
        state = fake_env_obj.state()
        assert any(full.startswith("hubinetops@pve!") for full in state["pve_tokens"])
        # The parent user must be preserved too -- not deleted.
        assert not any(line.startswith("pveum user delete") for line in log)
        assert "PRESERVING PVE user" in result.stderr
        assert "hubinetops@pve" in state["pve_users"]


# ---------------------------------------------------------------------------
# Identity pre-existing-conflict inspection must be fail-closed (ADDITIONAL
# P2, third pass): command failure or malformed JSON while checking whether
# hubinetops@pve / HubinetOpsR0Auditor already exist must never be silently
# read as "absent, safe to proceed" -- it must STOP before any mutation.
# ---------------------------------------------------------------------------


class TestIdentityInspectionFailClosed:
    def test_user_list_command_error_stops_before_user_add(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout, scenario_overrides={"fail": ["pveum_user_list"]}
        )
        assert result.returncode != 0
        assert not any(line.startswith("pveum user add") for line in fake_env_obj.log_lines())
        assert "could not verify whether" in result.stderr

    def test_malformed_user_list_json_stops_before_user_add(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={"malformed_pveum_output": {"user_list": True}},
        )
        assert result.returncode != 0
        assert not any(line.startswith("pveum user add") for line in fake_env_obj.log_lines())
        assert "could not verify whether" in result.stderr

    def test_role_list_command_error_stops_before_any_mutation(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout, scenario_overrides={"fail": ["pveum_role_list"]}
        )
        assert result.returncode != 0
        log = fake_env_obj.log_lines()
        assert not any(line.startswith("pveum user add") for line in log)
        assert not any(line.startswith("pveum role add") for line in log)
        assert "could not verify whether" in result.stderr

    def test_malformed_role_list_json_stops_before_any_mutation(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={"malformed_pveum_output": {"role_list": True}},
        )
        assert result.returncode != 0
        log = fake_env_obj.log_lines()
        assert not any(line.startswith("pveum user add") for line in log)
        assert not any(line.startswith("pveum role add") for line in log)
        assert "could not verify whether" in result.stderr

    def test_genuine_valid_empty_lists_still_allow_creation(self, tmp_path, source_checkout):
        # Positive control: the ordinary fresh-install case (both lists
        # genuinely empty, no command failure, valid JSON) must still
        # proceed normally -- the fail-closed fix must not make an
        # entirely healthy listing look like a conflict.
        result, fake_env_obj = _run_full(tmp_path, source_checkout)
        assert result.returncode == 0, result.stderr
        assert any(line.startswith("pveum user add") for line in fake_env_obj.log_lines())

    # -----------------------------------------------------------------
    # Schema validation (fourth pass): a syntactically valid JSON array
    # with an unexpected element shape ([{}], wrong field name, bare
    # strings/numbers, wrong field type) must be treated exactly like
    # malformed JSON -- a hard STOP before any mutation -- never silently
    # interpreted as "target not found, safe to proceed" merely because
    # the expected field wasn't where the field-lookup helper looked for
    # it.
    # -----------------------------------------------------------------

    @pytest.mark.parametrize(
        "bad_user_list",
        ["[{}]", '[{"user":"hubinetops@pve"}]', '["hubinetops@pve"]', "[1]", '[{"userid":123}]'],
    )
    def test_schema_invalid_user_list_stops_before_user_add(self, tmp_path, source_checkout, bad_user_list):
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={"pveum_output_override": {"user_list": bad_user_list}},
        )
        assert result.returncode != 0
        assert not any(line.startswith("pveum user add") for line in fake_env_obj.log_lines())
        assert "does not match the expected PVE JSON shape" in result.stderr

    @pytest.mark.parametrize(
        "bad_role_list",
        ['[{"role":"HubinetOpsR0Auditor"}]', '[{"roleid":123}]'],
    )
    def test_schema_invalid_role_list_stops_before_any_mutation(self, tmp_path, source_checkout, bad_role_list):
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={"pveum_output_override": {"role_list": bad_role_list}},
        )
        assert result.returncode != 0
        log = fake_env_obj.log_lines()
        assert not any(line.startswith("pveum user add") for line in log)
        assert not any(line.startswith("pveum role add") for line in log)
        assert "does not match the expected PVE JSON shape" in result.stderr

    def test_schema_valid_but_target_absent_still_proceeds(self, tmp_path, source_checkout):
        # Positive control at the new, stronger schema layer: a
        # well-formed row for a DIFFERENT user/role must still be read as
        # "target absent, proceed" -- the schema check must not be
        # over-strict either.
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={
                "pveum_output_override": {
                    "user_list": '[{"userid":"other@pve"}]',
                    "role_list": '[{"roleid":"OtherRole"}]',
                },
            },
        )
        assert result.returncode == 0, result.stderr
        assert any(line.startswith("pveum user add") for line in fake_env_obj.log_lines())

    def test_schema_invalid_token_permissions_stops_verification(self, tmp_path, source_checkout):
        # A JSON array instead of the expected flat {"Priv": 1, ...} object
        # must be an explicit STOP, never accidentally interpreted as an
        # empty/partial privilege set that merely happens to also fail the
        # exact-set comparison for an unrelated reason.
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            scenario_overrides={"pveum_output_override": {"token_permissions": '["Sys.Audit","VM.Audit"]'}},
        )
        assert result.returncode != 0
        assert "did not produce a valid JSON object" in result.stderr


# ---------------------------------------------------------------------------
# DNS resolver authority (P2-2, third pass): --dns-resolver is authoritative
# for the fresh container (pct create --nameserver), and the firewall's
# permitted DNS destination must be structurally proven -- via a live
# read-back of both the container's PVE config and its actual
# /etc/resolv.conf -- to match the resolver the container will really use,
# never merely asserted to match.
# ---------------------------------------------------------------------------


class TestDnsResolverAuthority:
    _HOSTNAME_ENDPOINT = "https://pve.example.invalid:8006"
    _RESOLVER = "198.51.100.53"

    def _args(self, **extra_scenario):
        scenario = {"dns_resolution": {"pve.example.invalid": ["203.0.113.10"]}}
        scenario.update(extra_scenario)
        return dict(
            args=["--pve-endpoint", self._HOSTNAME_ENDPOINT, "--dns-resolver", self._RESOLVER],
            scenario_overrides=scenario,
        )

    def test_declared_resolver_becomes_authoritative_nameserver_at_create_time(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(tmp_path, source_checkout, **self._args())
        assert result.returncode == 0, result.stderr
        assert any(
            "pct create" in line and f"--nameserver {self._RESOLVER}" in line
            for line in fake_env_obj.log_lines()
        )

    def test_declared_and_actual_resolver_match_hostname_mode_works(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(tmp_path, source_checkout, **self._args())
        assert result.returncode == 0, result.stderr
        assert "Discovery:            PASS" in result.stdout

    def test_mismatch_cannot_progress_to_service_start(self, tmp_path, source_checkout):
        # The container's actual live resolver differs from the declared
        # --dns-resolver (e.g. an out-of-band change, or a template
        # defaulting to a different resolver) -- must be a hard stop
        # before the firewall is generated, and therefore before the
        # service is ever started.
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            **self._args(ct_actual_resolv_conf="nameserver 192.168.1.1\n"),
        )
        assert result.returncode != 0
        log = fake_env_obj.log_lines()
        assert not any("systemctl enable --now hubinet-ops" in line for line in log)
        assert not any(line.startswith("pct push") and "nftables.conf" in line for line in log)
        assert "do not exactly match the declared --dns-resolver" in result.stderr

    def test_pct_config_missing_declared_nameserver_is_a_hard_stop(self, tmp_path, source_checkout):
        # Simulates PVE having silently ignored/rejected --nameserver at
        # create time (the container's own config never recorded it) --
        # must not be papered over by trusting /etc/resolv.conf alone.
        result, fake_env_obj = _run_full(tmp_path, source_checkout, **self._args(fail=["pct_config"]))
        assert result.returncode != 0
        assert "could not read back container" in result.stderr

    def test_zero_live_nameserver_entries_is_a_hard_stop(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout, **self._args(ct_actual_resolv_conf="")
        )
        assert result.returncode != 0
        assert "declares no usable nameserver entries" in result.stderr

    def test_unreadable_resolv_conf_is_a_hard_stop(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout, **self._args(ct_actual_resolv_conf=False)
        )
        assert result.returncode != 0
        assert "could not read /etc/resolv.conf" in result.stderr

    def test_literal_ip_mode_unaffected_no_resolver_verification_needed(self, tmp_path, source_checkout):
        # Default endpoint is a literal IP -- no --dns-resolver, no
        # --nameserver, no resolver verification at all.
        result, fake_env_obj = _run_full(tmp_path, source_checkout)
        assert result.returncode == 0, result.stderr
        assert not any("--nameserver" in line for line in fake_env_obj.log_lines() if line.startswith("pct create"))


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
        # Real-PVE corrective note (sixth pass): generation now writes the
        # CANONICAL nft address expression directly (FAKE_HA_SOURCE_CIDR
        # is a /32, so its canonical form is the bare address with no
        # suffix) -- not the operator's literal --ha-source text.
        assert f"ip saddr {FAKE_HA_SOURCE_CANONICAL} tcp dport 8787 accept" in ruleset
        assert ruleset.index(f"ip saddr {FAKE_HA_SOURCE_CANONICAL}") < ruleset.index("tcp dport 8787 drop")

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

    def test_loopback_accept_is_first_input_rule(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(tmp_path, source_checkout)
        assert result.returncode == 0, result.stderr
        ruleset = fake_env_obj.ct_file_text("110", "/etc/nftables.conf")
        assert 'iifname "lo" accept' in ruleset
        assert ruleset.index('iifname "lo" accept') < ruleset.index(f"ip saddr {FAKE_HA_SOURCE_CANONICAL}")

    def test_established_related_accept_is_first_output_rule(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(tmp_path, source_checkout)
        assert result.returncode == 0, result.stderr
        ruleset = fake_env_obj.ct_file_text("110", "/etc/nftables.conf")
        assert "ct state established,related accept" in ruleset
        pve_rule_idx = ruleset.index('meta skuid "hubinetops" ip daddr')
        assert ruleset.index("ct state established,related accept") < pve_rule_idx
        assert pve_rule_idx < ruleset.rindex('meta skuid "hubinetops" drop')

    # -----------------------------------------------------------------
    # Real-PVE canonicalization (sixth pass, first real dogfood on
    # Proxmox VE 9.2.3 / nftables 1.1.3): the real active-ruleset round
    # trip canonicalizes a /32 HA source to a bare address and a symbolic
    # `meta skuid "hubinetops"` to hubinetops' numeric UID -- these run
    # the REAL, corrected bootstrap script end to end (never a hand-
    # constructed fixture) to prove both the generated file and the
    # simulated active round trip agree with what a real host reported.
    # -----------------------------------------------------------------

    def test_ha_32_source_end_to_end_matches_real_dogfood_witness(self, tmp_path, source_checkout):
        # The exact --ha-source value from the real first dogfood run.
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            args=["--ha-source", "192.168.4.168/32"],
        )
        assert result.returncode == 0, result.stderr
        # Generation already writes the canonical (bare) form directly --
        # not the operator's literal /32 text -- so the file on disk and
        # the active ruleset always agree.
        ruleset = fake_env_obj.ct_file_text("110", "/etc/nftables.conf")
        assert "ip saddr 192.168.4.168 tcp dport 8787 accept" in ruleset
        assert "192.168.4.168/32" not in ruleset

    def test_non_32_ha_source_end_to_end_canonicalizes_to_network_form(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(
            tmp_path, source_checkout,
            args=["--ha-source", "192.168.4.168/24"],
        )
        assert result.returncode == 0, result.stderr
        ruleset = fake_env_obj.ct_file_text("110", "/etc/nftables.conf")
        assert "ip saddr 192.168.4.0/24 tcp dport 8787 accept" in ruleset
        assert "192.168.4.168/24" not in ruleset

    def test_symbolic_generation_and_numeric_uid_active_representation(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(tmp_path, source_checkout)
        assert result.returncode == 0, result.stderr
        # GENERATED file (pushed to the CT, pre-canonicalization) keeps
        # the symbolic name -- more readable, and real nft accepts it as
        # valid input.
        generated = fake_env_obj.ct_file_text("110", "/etc/nftables.conf")
        assert 'meta skuid "hubinetops"' in generated
        # The ACTIVE ruleset (the simulated `nft list ruleset` round
        # trip) shows the numeric UID instead -- exactly what a real host
        # reported, and exactly what Phase 10/12's own verification
        # (already proven to pass above) requires.
        active = subprocess.run(
            ["bash", "-c", "pct exec 110 -- nft list ruleset"],
            env=fake_env_obj.env, capture_output=True, text=True, timeout=15,
        ).stdout
        assert f"meta skuid {FAKE_HUBINETOPS_UID}" in active
        assert 'meta skuid "hubinetops"' not in active


# ---------------------------------------------------------------------------
# Firewall STATEFUL SEMANTICS (fifth-pass corrective fix, P2-1 whole-
# feature review): the OLD ruleset shape (no loopback accept in `input`,
# no established/related accept in `output`) silently dropped the
# bootstrap's own required Phase 12 loopback acceptance calls -- and even
# a loopback-only partial fix still silently dropped the R0 backend's own
# HTTP replies (it runs AS the hubinetops user) to either a loopback
# client or a real HA client, since those replies are hubinetops-owned
# OUTPUT packets with no established/related exemption ahead of the final
# "meta skuid hubinetops drop". The fake's own `curl`/discovery-accept
# simulators previously succeeded UNCONDITIONALLY, independent of what
# ruleset had actually been generated and activated -- exactly the kind
# of self-fulfilling test-double gap that let the original bug through
# 130+ passing smoke tests. tests/_bootstrap_fake_pve.py now enforces a
# bounded, order-aware structural invariant (NOT a full nftables
# emulator -- no real packet/conntrack simulation) on the ACTUAL active
# ruleset text before letting its curl/discovery-accept simulators report
# success. These tests exercise that mechanism directly against the
# OLD/partially-fixed/corrected shapes (proving the mechanism itself
# would have caught the original bug), then confirm the real, corrected
# bootstrap script's own end-to-end output satisfies every required
# property.
# ---------------------------------------------------------------------------

_OLD_BUGGY_RULESET = f'''table inet hubinet_ops_r0 {{
  chain input {{
    type filter hook input priority 0; policy accept;
    ip saddr {FAKE_HA_SOURCE_CIDR} tcp dport 8787 accept
    tcp dport 8787 drop
  }}
  chain output {{
    type filter hook output priority 0; policy accept;
    meta skuid "hubinetops" ip daddr {FAKE_PVE_ENDPOINT_HOST} tcp dport 8006 accept
    meta skuid "hubinetops" drop
  }}
}}
'''

_LOOPBACK_ONLY_RULESET = f'''table inet hubinet_ops_r0 {{
  chain input {{
    type filter hook input priority 0; policy accept;
    iifname "lo" accept
    ip saddr {FAKE_HA_SOURCE_CIDR} tcp dport 8787 accept
    tcp dport 8787 drop
  }}
  chain output {{
    type filter hook output priority 0; policy accept;
    meta skuid "hubinetops" ip daddr {FAKE_PVE_ENDPOINT_HOST} tcp dport 8006 accept
    meta skuid "hubinetops" drop
  }}
}}
'''

_CORRECTED_RULESET = f'''table inet hubinet_ops_r0 {{
  chain input {{
    type filter hook input priority 0; policy accept;
    iifname "lo" accept
    ip saddr {FAKE_HA_SOURCE_CIDR} tcp dport 8787 accept
    tcp dport 8787 drop
  }}
  chain output {{
    type filter hook output priority 0; policy accept;
    ct state established,related accept
    meta skuid "hubinetops" ip daddr {FAKE_PVE_ENDPOINT_HOST} tcp dport 8006 accept
    meta skuid "hubinetops" drop
  }}
}}
'''


class TestFirewallStatefulSemantics:
    def _activate(self, fake_env_obj, vmid, ruleset_text):
        """Directly writes `ruleset_text` to the simulated CT filesystem
        and "activates" it via the fake's own `pct exec <vmid> --
        systemctl restart nftables` handler -- mirrors what
        bootstrap-firewall.sh's phase10_firewall does (`pct push` then
        activate), but lets a test construct a ruleset shape the CURRENT,
        corrected production script would never itself generate anymore
        (the OLD/partially-fixed shapes), so the fake's own semantic gate
        can be exercised directly against them.
        """
        nft_path = fake_env_obj.ct_root / str(vmid) / "etc" / "nftables.conf"
        nft_path.parent.mkdir(parents=True, exist_ok=True)
        nft_path.write_text(ruleset_text, encoding="utf-8")
        result = subprocess.run(
            ["bash", "-c", f"pct exec {vmid} -- systemctl restart nftables"],
            env=fake_env_obj.env, capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == 0, result.stderr

    def _curl_health(self, fake_env_obj, vmid="110"):
        return subprocess.run(
            ["bash", "-c", f"pct exec {vmid} -- curl -fsS http://127.0.0.1:8787/r0/v1/health"],
            env=fake_env_obj.env, capture_output=True, text=True, timeout=15,
        )

    def _discovery_accept(self, fake_env_obj, vmid="110"):
        return subprocess.run(
            ["bash", "-c", f'pct exec {vmid} -- python3 /tmp/hubinet-ops-bootstrap-accept.py "{FAKE_DISPLAY_NAME}" 5'],
            env=fake_env_obj.env, capture_output=True, text=True, timeout=15,
        )

    # 1. The OLD (pre-fix) ruleset shape would fail acceptance.
    def test_old_ruleset_shape_fails_local_health_and_discovery(self, tmp_path):
        fake_env_obj = build_fake_pve_environment(tmp_path, default_scenario())
        self._activate(fake_env_obj, "110", _OLD_BUGGY_RULESET)
        health = self._curl_health(fake_env_obj)
        assert health.returncode != 0
        assert health.stdout == ""
        discovery = self._discovery_accept(fake_env_obj)
        assert discovery.returncode != 0
        assert "FAIL" in discovery.stdout

    # 2. A loopback-only partial fix (no established/related reply
    #    allowance) would ALSO fail -- the compounding half of the bug.
    def test_loopback_only_partial_fix_still_fails(self, tmp_path):
        fake_env_obj = build_fake_pve_environment(tmp_path, default_scenario())
        self._activate(fake_env_obj, "110", _LOOPBACK_ONLY_RULESET)
        health = self._curl_health(fake_env_obj)
        assert health.returncode != 0
        assert health.stdout == ""
        discovery = self._discovery_accept(fake_env_obj)
        assert discovery.returncode != 0
        assert "FAIL" in discovery.stdout

    # 3. The corrected rules allow local process-health acceptance.
    def test_corrected_ruleset_allows_local_process_health(self, tmp_path):
        fake_env_obj = build_fake_pve_environment(tmp_path, default_scenario())
        self._activate(fake_env_obj, "110", _CORRECTED_RULESET)
        health = self._curl_health(fake_env_obj)
        assert health.returncode == 0, health.stderr
        assert health.stdout != ""

    # 4. The corrected rules allow authenticated local discovery
    #    acceptance.
    def test_corrected_ruleset_allows_local_discovery_acceptance(self, tmp_path):
        fake_env_obj = build_fake_pve_environment(tmp_path, default_scenario())
        self._activate(fake_env_obj, "110", _CORRECTED_RULESET)
        discovery = self._discovery_accept(fake_env_obj)
        assert discovery.returncode == 0, discovery.stdout
        assert discovery.stdout.strip().splitlines()[-1].startswith("PASS")

    # 5. The corrected rules preserve HA ingress (never removed/narrowed
    #    while fixing loopback/replies).
    def test_corrected_ruleset_preserves_ha_ingress(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(tmp_path, source_checkout)
        assert result.returncode == 0, result.stderr
        ruleset = fake_env_obj.ct_file_text("110", "/etc/nftables.conf")
        assert f"ip saddr {FAKE_HA_SOURCE_CANONICAL} tcp dport 8787 accept" in ruleset

    # 6. The final hubinetops drop remains, exactly once, still last.
    def test_final_hubinetops_drop_remains_exactly_once_and_last(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(tmp_path, source_checkout)
        assert result.returncode == 0, result.stderr
        ruleset = fake_env_obj.ct_file_text("110", "/etc/nftables.conf")
        assert ruleset.count('meta skuid "hubinetops" drop') == 1
        output_lines = [
            line.strip() for line in ruleset.splitlines()
            if line.strip() and not line.strip().startswith(("table", "chain", "type filter", "}"))
        ]
        # The output chain's rules are exactly the tail of this filtered
        # list (input's rules come first) -- the final entry overall,
        # after firewall generation, must be the hubinetops drop.
        assert output_lines[-1] == 'meta skuid "hubinetops" drop'

    # 7. Arbitrary NEW hubinetops egress is still not allowed -- the
    #    established/related fix must not have widened into a general
    #    outbound allowance. Every rule naming the hubinetops skuid is
    #    either the final drop or an exact, narrow PVE/DNS destination
    #    accept; established/related itself is NOT skuid-scoped (so it
    #    cannot authorize a NEW hubinetops-initiated connection to an
    #    arbitrary destination -- only replies to flows already accepted
    #    by the input chain's own HA/loopback rules).
    def test_no_broadened_new_hubinetops_egress(self, tmp_path, source_checkout):
        result, fake_env_obj = _run_full(tmp_path, source_checkout)
        assert result.returncode == 0, result.stderr
        ruleset = fake_env_obj.ct_file_text("110", "/etc/nftables.conf")
        skuid_lines = [
            line.strip() for line in ruleset.splitlines()
            if 'meta skuid "hubinetops"' in line
        ]
        for line in skuid_lines:
            assert line == 'meta skuid "hubinetops" drop' or line.startswith(
                'meta skuid "hubinetops" ip daddr'
            ), f"unexpected/broadened hubinetops rule: {line}"
        # established/related is a plain ct-state rule, never scoped to
        # (or gated by) the hubinetops skuid -- confirms it cannot be
        # mistaken for a hubinetops-specific NEW-connection allowance.
        established_lines = [
            line.strip() for line in ruleset.splitlines()
            if "ct state established,related" in line
        ]
        assert len(established_lines) == 1
        assert 'meta skuid "hubinetops"' not in established_lines[0]


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
