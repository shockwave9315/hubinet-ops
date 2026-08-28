"""Local-safe tests for deploy/bootstrap-proxmox-0.5.sh.

Per AGENTS.md's deployment-script sandbox boundary, NO test in this file
ever executes deploy/bootstrap-proxmox-0.5.sh itself, or invokes any
`phase*`/orchestration function from a deploy/lib/bootstrap-*.sh module,
as a real subprocess -- not even against the hermetic fake-command PATH in
tests/_bootstrap_fake_pve.py. That category of test (the actual functional
matrix: preflight orchestration, container creation, PVE identity, TLS,
config generation, firewall, rollback, acceptance) lives in
tests/test_bootstrap_proxmox_0_5_smoke.py instead, which only ever runs
inside the Docker-based ephemeral-CI sandbox (see
tests/shell/run_bootstrap_smoke_sandbox.sh) and is a hard pytest skip
everywhere else -- see test_smoke_suite_is_sandbox_gated below, which
proves that structurally rather than by convention.

Narrow, deliberate exception: a handful of tests in this file (see
TestStorageFreeSpaceHelper) source ONLY bootstrap-common.sh plus a single
named, non-mutating, pure validation/arithmetic function from a lib module
(e.g. `_storage_has_free_space`) in a bash subshell, then call that one
function directly against a minimal single-purpose fake `pvesm` on an
isolated PATH -- never `phase1_preflight` itself, never any orchestration,
never a mutating pct/pveum/pveam call. This tests the exact unit-parsing
logic AGENTS.md's sandbox boundary is not concerned with (it targets real
privileged/deployment command execution, not bash arithmetic on a string),
while remaining strictly local-safe.

Everything else in this file is either: a pure syntax check (`bash -n`,
never executes script content), a lexical/static text scan of the script
source, or a check that the sandbox infrastructure itself is present and
consistent. tests/_bootstrap_fake_pve.py (the fake command layer used by
the sandboxed smoke suite) is imported here only for its constants/scenario
builders where useful for static assertions -- never to actually run the
target script.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap_fake_pve import (  # noqa: E402
    FAKE_DISPLAY_NAME,
    FAKE_HA_SOURCE_CIDR,
    FAKE_PVE_ENDPOINT,
    build_fake_pve_environment,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_SCRIPT = REPO_ROOT / "deploy" / "bootstrap-proxmox-0.5.sh"
LIB_DIR = REPO_ROOT / "deploy" / "lib"
ALL_SCRIPTS = [BOOTSTRAP_SCRIPT, *sorted(LIB_DIR.glob("*.sh"))]
ACCEPT_SCRIPT_PY = LIB_DIR / "hubinet-ops-bootstrap-accept.py"
SMOKE_TEST_FILE = REPO_ROOT / "tests" / "test_bootstrap_proxmox_0_5_smoke.py"
VALIDATOR = REPO_ROOT / "scripts" / "validate_hermetic_shell_boundary.py"
SANDBOX_DOCKERFILE = REPO_ROOT / "tests" / "shell" / "Dockerfile.bootstrap-smoke"
SANDBOX_RUNNER = REPO_ROOT / "tests" / "shell" / "run_bootstrap_smoke_sandbox.sh"
SANDBOX_ENTRYPOINT = REPO_ROOT / "tests" / "shell" / "bootstrap_smoke_sandbox_entrypoint.sh"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

pytestmark = pytest.mark.skipif(
    __import__("shutil").which("bash") is None, reason="bash is not available in this environment"
)


def _code_only(script: Path) -> str:
    """Script text with comment-only lines stripped, so a lexical check
    for a forbidden pattern doesn't false-positive on this repository's
    own explanatory prose (e.g. a comment describing an anti-pattern that
    was fixed, or naming the exact regression witness a test guards
    against). Not a real shell parser -- good enough for these
    line-oriented checks since every comment in these scripts starts a
    line with '#' (no inline trailing '# comment' after real code in
    these files' style).
    """
    return "\n".join(
        line for line in script.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("#")
    )


# ---------------------------------------------------------------------------
# Sandbox boundary self-guards -- prove the P1 finding is structurally
# closed, not merely fixed by convention.
# ---------------------------------------------------------------------------


def test_this_file_never_spawns_the_real_bootstrap_script():
    """An AST-based self-guard (robust to formatting, unlike a plain
    substring scan): every subprocess.run/Popen call in this file whose
    argv references a bootstrap script variable (BOOTSTRAP_SCRIPT, ALL_SCRIPTS,
    the parametrized `script` fixture value, ACCEPT_SCRIPT_PY) must also
    pass a syntax-only flag ("-n" for bash, "py_compile" for python) --
    proving structurally that this file never executes real script content,
    which would silently reintroduce the P1 violation this split exists to
    close.
    """
    import ast

    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"), filename=str(__file__))
    script_referencing_names = {"BOOTSTRAP_SCRIPT", "ALL_SCRIPTS", "script", "ACCEPT_SCRIPT_PY"}

    def _dump_contains_any(node, names):
        return any(name in ast.dump(node) for name in names)

    checked_any = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_subprocess_call = (
            isinstance(func, ast.Attribute)
            and func.attr in ("run", "Popen")
            and isinstance(func.value, ast.Name)
            and func.value.id == "subprocess"
        )
        if not is_subprocess_call or not node.args:
            continue
        argv_node = node.args[0]
        if not _dump_contains_any(argv_node, script_referencing_names):
            continue
        checked_any = True
        dumped = ast.dump(argv_node)
        # Safe forms: `bash -n <script>` (syntax-only), `python -m
        # py_compile <script>` (syntax-only), or running OUR OWN lexical
        # validator against the script's text (never executes it) --
        # anything else referencing a bootstrap script variable as a
        # subprocess argv is exactly the P1 pattern this test exists to
        # catch.
        is_safe = "'-n'" in dumped or "py_compile" in dumped or "VALIDATOR" in dumped
        assert is_safe, (
            "a subprocess call references a bootstrap script without a "
            "recognized safe form ('-n'/'py_compile'/VALIDATOR) -- this "
            "file must never execute real script content"
        )
    assert checked_any, "expected at least one syntax-only subprocess call to exist in this file"


def test_smoke_suite_is_sandbox_gated():
    """The file that DOES execute the real script must hard-skip outside
    the sandbox marker -- checked lexically here so a future edit to that
    file that removes the gate is itself caught by a local-safe test.
    """
    assert SMOKE_TEST_FILE.exists(), "sandboxed bootstrap smoke-test file is missing"
    text = SMOKE_TEST_FILE.read_text(encoding="utf-8")
    assert "HUBINET_OPS_SYSTEM_SANDBOX" in text
    assert "pytest.mark.skipif" in text


def test_sandbox_infrastructure_files_exist():
    for path in (SANDBOX_DOCKERFILE, SANDBOX_RUNNER, SANDBOX_ENTRYPOINT):
        assert path.exists(), f"missing sandbox infrastructure file: {path}"


# ---------------------------------------------------------------------------
# CI wiring: the real Docker sandbox must be a normal job in the repository's
# one post-merge/manual workflow and must never bypass the launcher by setting
# HUBINET_OPS_SYSTEM_SANDBOX directly.
# ---------------------------------------------------------------------------


def _load_ci_workflow() -> dict:
    import yaml

    with CI_WORKFLOW.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _ci_workflow_triggers() -> dict:
    doc = _load_ci_workflow()
    # YAML parses the bare `on:` key as the boolean True key under Python's
    # yaml.safe_load (YAML 1.1 boolean-like scalar) -- look it up either way.
    return doc.get("on", doc.get(True))


def test_ci_workflow_exists_and_is_valid_yaml():
    assert CI_WORKFLOW == REPO_ROOT / ".github" / "workflows" / "ci.yml"
    assert CI_WORKFLOW.exists(), "the single CI workflow is missing"
    assert isinstance(_load_ci_workflow(), dict)


def test_ci_directory_contains_only_the_single_ci_workflow():
    workflows = sorted(path.name for path in CI_WORKFLOW.parent.iterdir() if path.is_file())
    assert workflows == ["ci.yml"]


def test_ci_workflow_runs_only_on_main_push_or_manual_dispatch():
    triggers = _ci_workflow_triggers()
    assert set(triggers) == {"push", "workflow_dispatch"}
    assert triggers["push"] == {"branches": ["main"]}
    assert "pull_request" not in triggers


def test_ci_workflow_invokes_the_launcher_and_nothing_else():
    smoke = _load_ci_workflow()["jobs"]["bootstrap-smoke"]
    run_steps = [step["run"] for step in smoke["steps"] if "run" in step]
    assert run_steps == ["bash tests/shell/run_bootstrap_smoke_sandbox.sh"]

    # The launcher must remain the single system-enforced entry boundary --
    # this workflow must never set HUBINET_OPS_SYSTEM_SANDBOX itself (only
    # the launcher, from inside its own Docker container, may do that).
    # Comment-only lines are stripped first so this doesn't false-positive
    # on this file's own explanatory prose naming that exact anti-pattern.
    text = CI_WORKFLOW.read_text(encoding="utf-8")
    code_text = "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("#")
    )
    assert "HUBINET_OPS_SYSTEM_SANDBOX" not in code_text


def test_ci_workflow_sets_the_required_ephemeral_ci_marker():
    smoke = _load_ci_workflow()["jobs"]["bootstrap-smoke"]
    assert smoke["runs-on"] == "ubuntu-latest"
    assert smoke["env"]["HUBINET_OPS_EPHEMERAL_CI"] == "1"


def test_sandbox_runner_fails_closed_outside_ephemeral_ci():
    text = SANDBOX_RUNNER.read_text(encoding="utf-8")
    for marker in (
        "GITHUB_ACTIONS",
        "HUBINET_OPS_EPHEMERAL_CI",
        "RUNNER_ENVIRONMENT",
        "GITHUB_RUN_ID",
    ):
        assert marker in text, f"sandbox runner does not check {marker}"
    for isolation_flag in (
        "--network none",
        "--read-only",
        "--cap-drop ALL",
        "no-new-privileges=true",
        "--ipc none",
        "--pids-limit",
        "--memory",
        "--cpus",
        "--user 65534:65534",
    ):
        assert isolation_flag in text, f"sandbox runner is missing isolation flag: {isolation_flag}"


def test_sandbox_runner_never_executed_directly_by_pytest():
    # Defense in depth: no test anywhere in this local-safe file invokes
    # run_bootstrap_smoke_sandbox.sh (that would require Docker and is
    # gated to ephemeral CI only, per the skip above) -- only referenced
    # for its own existence/content checks (.exists()/.read_text()).
    import ast

    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"), filename=str(__file__))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_process_spawn = (
            isinstance(func, ast.Attribute)
            and (
                (isinstance(func.value, ast.Name) and func.value.id in ("subprocess", "os"))
            )
            and func.attr in ("run", "Popen", "system", "call", "check_call", "check_output")
        )
        if not is_process_spawn:
            continue
        assert "SANDBOX_RUNNER" not in ast.dump(node), (
            "SANDBOX_RUNNER must never be passed to a process-spawning call "
            "from this local-safe file"
        )


def test_hermetic_shell_boundary_validator_restored():
    assert VALIDATOR.exists(), "scripts/validate_hermetic_shell_boundary.py must exist (restored static validator)"


@pytest.mark.parametrize("script", ALL_SCRIPTS)
def test_hermetic_shell_boundary_clean(script):
    # Defense-in-depth lexical check (NOT the real sandbox boundary --
    # that's tests/shell/run_bootstrap_smoke_sandbox.sh + Docker): rejects
    # absolute standard-executable-path invocation and Bash network
    # devices that would bypass the fake-command PATH the sandboxed smoke
    # suite relies on for hermeticity.
    result = subprocess.run(
        [sys.executable, str(VALIDATOR), str(script)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# Static syntax check -- `bash -n` / `python -m py_compile` never execute
# script content.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("script", ALL_SCRIPTS)
def test_syntax_is_valid(script):
    result = subprocess.run(
        ["bash", "-n", str(script)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr


def test_accept_script_syntax_is_valid():
    result = subprocess.run(
        [sys.executable, "-m", "py_compile", str(ACCEPT_SCRIPT_PY)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# Pure-helper unit tests -- source ONLY bootstrap-common.sh + the single
# named function under test, never a phase/orchestration entrypoint. See
# this file's module docstring for why this stays local-safe.
# ---------------------------------------------------------------------------


def _run_pure_helper(tmp_path, call_expr, *, pvesm_output=None, pvesm_exit=0):
    """Source bootstrap-common.sh + bootstrap-preflight.sh (function
    definitions only -- inert to source), install a minimal single-purpose
    fake `pvesm` on an isolated PATH, then evaluate `call_expr` (expected
    to be exactly one call to a named pure helper function) and return the
    completed subprocess. No phase function, no orchestration, no other
    privileged command is ever invoked.
    """
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    pvesm_shim = bin_dir / "pvesm"
    if pvesm_output is None:
        body = f'exit {pvesm_exit}\n'
    else:
        body = f"cat <<'PVESM_EOF'\n{pvesm_output}\nPVESM_EOF\nexit {pvesm_exit}\n"
    pvesm_shim.write_text(f"#!/usr/bin/env bash\n{body}", encoding="utf-8")
    pvesm_shim.chmod(0o755)

    # Deliberately not named `script` -- that identifier is reserved by
    # test_this_file_never_spawns_the_real_bootstrap_script's AST guard
    # below to mean "a bootstrap script path/variable"; this is a small,
    # inline, function-only bash snippet with no relation to the real
    # production script and must not trip that guard.
    bash_snippet = (
        f'source "{(LIB_DIR / "bootstrap-common.sh").as_posix()}"\n'
        f'source "{(LIB_DIR / "bootstrap-preflight.sh").as_posix()}"\n'
        f"{call_expr}\n"
        "exit $?\n"
    )
    import os as _os

    env = dict(_os.environ)
    env["PATH"] = f"{bin_dir}{_os.pathsep}{env.get('PATH', '')}"
    return subprocess.run(
        ["bash", "-c", bash_snippet], capture_output=True, text=True, timeout=15, env=env,
    )


class TestStorageFreeSpaceHelper:
    """Mandatory storage-KiB fix: `pvesm status` reports Total/Used/
    Available in KiB, not bytes. A prior version of `_storage_has_free_space`
    compared a byte-scaled requirement against the raw KiB value directly,
    understating available space by ~1024x (an 8 GiB request was effectively
    compared as if it needed ~8 TiB free) -- these tests pin the corrected
    KiB-to-KiB comparison and the fail-closed-on-unparseable behavior using
    only this one pure function, never the full bootstrap script.
    """

    _HEADER = "Name Type Status Total Used Available %"

    def test_realistic_kib_available_passes(self, tmp_path):
        # 100 GiB available (in KiB) satisfies an 8 GiB request.
        avail_kib = 100 * 1024 * 1024
        output = f"{self._HEADER}\nfakestorage dir active 200000000 100000000 {avail_kib} 1.00"
        result = _run_pure_helper(
            tmp_path, '_storage_has_free_space "fakestorage" 8', pvesm_output=output,
        )
        assert result.returncode == 0, result.stderr

    def test_insufficient_kib_available_fails(self, tmp_path):
        # 7 GiB available (in KiB), 8 GiB requested.
        avail_kib = 7 * 1024 * 1024
        output = f"{self._HEADER}\nfakestorage dir active 200000000 193000000 {avail_kib} 96.5"
        result = _run_pure_helper(
            tmp_path, '_storage_has_free_space "fakestorage" 8', pvesm_output=output,
        )
        assert result.returncode != 0

    def test_malformed_available_value_fails_closed(self, tmp_path):
        # A prior version logged a warning and returned success (silently
        # SKIPPING the check) on an unparseable value -- that false-pass
        # must never happen again.
        output = f"{self._HEADER}\nfakestorage dir active 200000000 N/A N/A N/A"
        result = _run_pure_helper(
            tmp_path, '_storage_has_free_space "fakestorage" 8', pvesm_output=output,
        )
        assert result.returncode != 0
        assert "could not reliably parse available free space" in result.stderr

    def test_borderline_kib_available_is_a_pass_at_exact_equality(self, tmp_path):
        # 8 GiB available (in KiB) for an 8 GiB request -- the boundary
        # condition of the ">=" comparison.
        avail_kib = 8 * 1024 * 1024
        output = f"{self._HEADER}\nfakestorage dir active 200000000 192000000 {avail_kib} 96.0"
        result = _run_pure_helper(
            tmp_path, '_storage_has_free_space "fakestorage" 8', pvesm_output=output,
        )
        assert result.returncode == 0, result.stderr


def _run_json_schema_helper(tmp_path, call_expr, *, json_content):
    """Source ONLY bootstrap-common.sh (function definitions only -- inert
    to source), write `json_content` to a temp file, then evaluate
    `call_expr` (expected to be exactly one call to a named pure JSON
    schema-validation helper against that file) and return the completed
    subprocess. No PVE command, no phase function, no orchestration is
    ever invoked -- these helpers are pure functions of a file path.
    """
    json_file = tmp_path / "input.json"
    json_file.write_text(json_content, encoding="utf-8")

    bash_snippet = (
        f'source "{(LIB_DIR / "bootstrap-common.sh").as_posix()}"\n'
        f'{call_expr.format(json_file=json_file.as_posix())}\n'
        "exit $?\n"
    )
    import os as _os

    env = dict(_os.environ)
    # Windows Git-Bash only: MSYS auto-converts a bare "/"-shaped argument
    # (e.g. the PVE ACL root path "/", used by
    # _json_truthy_keys_sorted_at_path's callers) into a Windows path
    # (observed: the Git installation root) when it crosses into a native
    # (non-MSYS) executable such as python3.exe -- corrupting the literal
    # single-character argument the underlying helper actually needs.
    # This has no effect on Linux, where MSYS doesn't exist and this
    # variable is meaningless; existing file-path arguments constructed
    # via `.as_posix()` already carry a drive letter and are unaffected
    # either way.
    env["MSYS2_ARG_CONV_EXCL"] = "*"
    return subprocess.run(
        ["bash", "-c", bash_snippet], capture_output=True, text=True, timeout=15, env=env,
    )


class TestJsonSchemaHelpers:
    """Fourth-pass corrective fix: a JSON-array-only check (an earlier
    _json_list_is_valid) proved only "the top-level value is an array" --
    a syntactically valid but semantically wrong array such as [{}],
    [{"user": "..."}] (wrong field name), ["hubinetops@pve"] (bare
    strings), or [1] (non-object elements) would all pass that weaker
    check, and the field-lookup helper's own "no match found" result is
    then indistinguishable from a genuine absence -- fail-open for a
    security-relevant pre-existing-identity conflict check or rollback
    ownership read-back. These tests pin
    _json_list_has_string_field_schema's and _json_object_is_valid's
    exact accept/reject behavior directly, independent of the full
    bootstrap script.
    """

    @pytest.mark.parametrize(
        "json_content",
        [
            "[]",
            '[{"userid":"other@pve"}]',
            '[{"userid":"hubinetops@pve"}]',
            '[{"userid":"a@pve"},{"userid":"b@pve","comment":"extra optional field is fine"}]',
        ],
    )
    def test_valid_userid_schema_accepted(self, tmp_path, json_content):
        result = _run_json_schema_helper(
            tmp_path, '_json_list_has_string_field_schema "{json_file}" "userid"',
            json_content=json_content,
        )
        assert result.returncode == 0, result.stderr

    @pytest.mark.parametrize(
        "json_content",
        [
            "[{}]",
            '[{"user":"hubinetops@pve"}]',
            '["hubinetops@pve"]',
            "[1]",
            '[{"userid":123}]',
            '[{"userid":null}]',
            "{}",
            '{"userid":"hubinetops@pve"}',
            "not-valid-json{{{",
            "",
            '[{"userid":"a@pve"},{}]',
        ],
    )
    def test_invalid_userid_schema_rejected(self, tmp_path, json_content):
        result = _run_json_schema_helper(
            tmp_path, '_json_list_has_string_field_schema "{json_file}" "userid"',
            json_content=json_content,
        )
        assert result.returncode != 0

    @pytest.mark.parametrize(
        "json_content",
        [
            "[]",
            '[{"roleid":"OtherRole"}]',
            '[{"roleid":"HubinetOpsR0Auditor"}]',
        ],
    )
    def test_valid_roleid_schema_accepted(self, tmp_path, json_content):
        result = _run_json_schema_helper(
            tmp_path, '_json_list_has_string_field_schema "{json_file}" "roleid"',
            json_content=json_content,
        )
        assert result.returncode == 0, result.stderr

    @pytest.mark.parametrize(
        "json_content",
        [
            '[{"role":"HubinetOpsR0Auditor"}]',
            '[{"roleid":123}]',
        ],
    )
    def test_invalid_roleid_schema_rejected(self, tmp_path, json_content):
        result = _run_json_schema_helper(
            tmp_path, '_json_list_has_string_field_schema "{json_file}" "roleid"',
            json_content=json_content,
        )
        assert result.returncode != 0

    @pytest.mark.parametrize(
        "json_content",
        ['{"Sys.Audit":1,"VM.Audit":1}', "{}"],
    )
    def test_valid_object_schema_accepted(self, tmp_path, json_content):
        result = _run_json_schema_helper(
            tmp_path, '_json_object_is_valid "{json_file}"', json_content=json_content,
        )
        assert result.returncode == 0, result.stderr

    @pytest.mark.parametrize(
        "json_content",
        ["[]", '["Sys.Audit","VM.Audit"]', '"a string"', "1", "not-valid-json{{{", ""],
    )
    def test_invalid_object_schema_rejected(self, tmp_path, json_content):
        result = _run_json_schema_helper(
            tmp_path, '_json_object_is_valid "{json_file}"', json_content=json_content,
        )
        assert result.returncode != 0


class TestPathKeyedPermissionHelper:
    """Real-PVE corrective fix: `pveum user token permissions <user>
    <token> --path / --output-format json` was assumed to return a flat
    object of privilege names directly at the top level
    ({"Sys.Audit": 1, ...}). A real-host read-only precheck against
    Proxmox VE 9.2.3 disproved that -- the real command returns a
    PATH-KEYED object instead ({"/": {"Sys.Audit": 1, ...}}), observed
    literally as `{"/":{}}` for an empty grant (see
    docs/architecture/0.5-implementation-status.md's real-PVE precheck
    notes). These tests pin _json_truthy_keys_sorted_at_path's exact
    accept/reject behavior directly, independent of the full bootstrap
    script -- including explicitly proving the OLD flat-object shape is
    now rejected rather than silently misinterpreted as "zero
    privileges."
    """

    def _extract(self, tmp_path, json_content):
        return _run_json_schema_helper(
            tmp_path, '_json_truthy_keys_sorted_at_path "{json_file}" "/"',
            json_content=json_content,
        )

    def test_real_observed_empty_grant_is_a_valid_shape(self, tmp_path):
        # The literal real-PVE-9.2.3 observation for a token with no
        # privileges yet granted at "/": {"/":{}}
        result = self._extract(tmp_path, '{"/":{}}')
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == ""

    @pytest.mark.parametrize(
        "json_content",
        ['{"/":{"Sys.Audit":1,"VM.Audit":1}}', '{"/":{"VM.Audit":1,"Sys.Audit":1}}'],
    )
    def test_exact_grant_accepted_regardless_of_key_order(self, tmp_path, json_content):
        result = self._extract(tmp_path, json_content)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip().splitlines() == ["Sys.Audit", "VM.Audit"]

    def test_missing_required_privilege_extracted_as_the_smaller_set(self, tmp_path):
        # The helper itself only extracts what is truly granted at "/" --
        # the "missing required privilege" STOP is enforced by the
        # caller's own exact-set comparison (see the end-to-end smoke
        # test), not by this extraction helper.
        result = self._extract(tmp_path, '{"/":{"Sys.Audit":1}}')
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip().splitlines() == ["Sys.Audit"]

    def test_extra_privilege_included_verbatim(self, tmp_path):
        result = self._extract(tmp_path, '{"/":{"Sys.Audit":1,"VM.Audit":1,"VM.PowerMgmt":1}}')
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip().splitlines() == ["Sys.Audit", "VM.Audit", "VM.PowerMgmt"]

    @pytest.mark.parametrize(
        "json_content",
        [
            '{"/vms":{"Sys.Audit":1,"VM.Audit":1}}',  # wrong path key
            "[]",  # wrong root shape
            '{"/":[]}',  # "/" present but not an object
            '{"Sys.Audit":1,"VM.Audit":1}',  # the OLD, now-invalid flat assumption
            "not-valid-json{{{",  # malformed JSON
            "",  # empty file
        ],
    )
    def test_unexpected_shapes_rejected(self, tmp_path, json_content):
        result = self._extract(tmp_path, json_content)
        assert result.returncode != 0


def _run_nft_canonical_ha_helper(tmp_path, cidr):
    """Source ONLY bootstrap-common.sh + bootstrap-firewall.sh (function
    definitions only -- inert to source), then evaluate exactly one call
    to _nft_canonical_ha_source_expr against `cidr` and return the
    completed subprocess. No PVE command, no phase function, no
    orchestration is ever invoked -- this is a pure function of a CIDR
    string.
    """
    bash_snippet = (
        f'source "{(LIB_DIR / "bootstrap-common.sh").as_posix()}"\n'
        f'source "{(LIB_DIR / "bootstrap-firewall.sh").as_posix()}"\n'
        f'_nft_canonical_ha_source_expr "{cidr}"\n'
        "exit $?\n"
    )
    return subprocess.run(
        ["bash", "-c", bash_snippet], capture_output=True, text=True, timeout=15,
    )


class TestNftCanonicalHaSourceExpr:
    """Real-PVE corrective fix (first real dogfood, Proxmox VE 9.2.3 /
    nftables 1.1.3): nftables canonicalizes `ip saddr` address expressions
    on the active-ruleset round trip -- a /32 host CIDR is displayed
    WITHOUT its /32 suffix, and any other prefix is displayed as the
    canonical NETWORK address for that prefix, never the operator's
    literal address+prefix text. These tests pin
    _nft_canonical_ha_source_expr's exact output directly, independent of
    the full bootstrap script.
    """

    @pytest.mark.parametrize(
        "cidr,expected",
        [
            ("192.168.4.168/32", "192.168.4.168"),  # the exact real dogfood witness
            ("203.0.113.50/32", "203.0.113.50"),
            ("10.0.0.1/32", "10.0.0.1"),
        ],
    )
    def test_host_32_canonicalizes_to_bare_address(self, tmp_path, cidr, expected):
        result = _run_nft_canonical_ha_helper(tmp_path, cidr)
        assert result.returncode == 0, result.stderr
        assert result.stdout == expected

    @pytest.mark.parametrize(
        "cidr,expected",
        [
            ("192.168.4.168/24", "192.168.4.0/24"),  # host address, non-canonical input
            ("192.168.4.0/24", "192.168.4.0/24"),  # already canonical
            ("10.5.5.5/16", "10.5.0.0/16"),
            ("10.0.0.0/8", "10.0.0.0/8"),
        ],
    )
    def test_non_32_canonicalizes_to_network_form(self, tmp_path, cidr, expected):
        result = _run_nft_canonical_ha_helper(tmp_path, cidr)
        assert result.returncode == 0, result.stderr
        assert result.stdout == expected


def _run_hubinetops_uid_helper(tmp_path, *, id_output, id_exit=0):
    """Source ONLY bootstrap-common.sh + bootstrap-firewall.sh, provide a
    minimal fake `pct` bash function simulating `pct exec <vmid> -- id -u
    hubinetops`, then call _hubinetops_uid directly. No real PVE host is
    ever contacted -- "pct" here is a hand-written bash stub entirely
    local to this one test process.
    """
    bash_snippet = f'''
source "{(LIB_DIR / "bootstrap-common.sh").as_posix()}"
source "{(LIB_DIR / "bootstrap-firewall.sh").as_posix()}"
VMID="110"

pct() {{
  printf '%s' "{id_output}"
  return {id_exit}
}}

_hubinetops_uid
exit $?
'''
    return subprocess.run(
        ["bash", "-c", bash_snippet], capture_output=True, text=True, timeout=15,
    )


class TestHubinetopsUidHelper:
    """Real-PVE corrective fix: real nftables also resolves a symbolic
    `meta skuid "hubinetops"` match expression to hubinetops' numeric UID
    at rule-load time, and reports only that numeric UID back on the
    active-ruleset round trip -- never hardcoded, always read back from
    the target container via `pct exec <vmid> -- id -u hubinetops`,
    strictly validated as a bare numeric UID.
    """

    def test_valid_numeric_uid_accepted(self, tmp_path):
        result = _run_hubinetops_uid_helper(tmp_path, id_output="999\n")
        assert result.returncode == 0, result.stderr
        assert result.stdout == "999"

    def test_command_failure_stops(self, tmp_path):
        result = _run_hubinetops_uid_helper(tmp_path, id_output="", id_exit=1)
        assert result.returncode != 0
        assert "could not determine the numeric UID" in result.stderr

    def test_malformed_output_stops(self, tmp_path):
        result = _run_hubinetops_uid_helper(tmp_path, id_output="not-a-uid\n")
        assert result.returncode != 0
        assert "unexpected output from" in result.stderr

    def test_empty_output_stops(self, tmp_path):
        result = _run_hubinetops_uid_helper(tmp_path, id_output="")
        assert result.returncode != 0
        assert "unexpected output from" in result.stderr


# The exact active-ruleset text a real first dogfood run (Proxmox VE
# 9.2.3 / nftables 1.1.3) reported via `nft list ruleset` -- the literal
# witness that exposed both canonicalization bugs. Note the real host's
# own "priority filter" text (vs. this bootstrap's generated "priority
# 0") -- irrelevant to verification, which always skips the "type filter
# hook ..." line entirely regardless of its exact content.
_REAL_HOST_FIXTURE = '''table inet hubinet_ops_r0 {
  chain input {
    type filter hook input priority filter; policy accept;
    iifname "lo" accept
    ip saddr 192.168.4.168 tcp dport 8787 accept
    tcp dport 8787 drop
  }

  chain output {
    type filter hook output priority filter; policy accept;
    ct state established,related accept
    meta skuid 999 ip daddr 192.168.4.249 tcp dport 8006 accept
    meta skuid 999 drop
  }
}
'''

_REAL_HOST_CONFIG = dict(
    ha_source_cidr="192.168.4.168/32",
    pve_endpoint="https://192.168.4.249:8006",
    resolved_pve_ips=["192.168.4.249"],
    hubinetops_uid="999",
)


def _run_firewall_verify_helper(
    tmp_path, *, ha_source_cidr, pve_endpoint, resolved_pve_ips, hubinetops_uid,
    active_ruleset_text, dns_resolver_ip="",
):
    """Source ONLY bootstrap-common.sh + bootstrap-firewall.sh, provide a
    minimal fake `pct` bash function returning the exact given (already
    real-host-shaped) active ruleset text and hubinetops UID, then call
    _verify_firewall_active directly against the given static config.
    Proves the verifier's behavior against a hand-supplied "what a real
    host actually reported" fixture, independent of the full bootstrap
    script and without ever contacting a real PVE host. `tmp_path` is a
    fresh, unique directory per test invocation (pytest fixture), so a
    fixed filename within it is safe -- each test calls this helper at
    most once.
    """
    nft_output_file = tmp_path / "nft_output.txt"
    nft_output_file.write_text(active_ruleset_text, encoding="utf-8")
    resolved_ips_arg = "\\n".join(resolved_pve_ips)

    bash_snippet = f'''
source "{(LIB_DIR / "bootstrap-common.sh").as_posix()}"
source "{(LIB_DIR / "bootstrap-firewall.sh").as_posix()}"
VMID="110"
HA_SOURCE_CIDR="{ha_source_cidr}"
PVE_ENDPOINT="{pve_endpoint}"
DNS_RESOLVER_IP="{dns_resolver_ip}"
RESOLVED_PVE_IPS_LIST=$'{resolved_ips_arg}'

pct() {{
  if [[ "$1" == "exec" && "$2" == "110" && "$3" == "--" ]]; then
    if [[ "$4 $5" == "nft list" ]]; then
      cat "{nft_output_file.as_posix()}"
      return 0
    fi
    if [[ "$4 $5" == "id -u" ]]; then
      printf '%s\\n' "{hubinetops_uid}"
      return 0
    fi
  fi
  return 2
}}

_verify_firewall_active
exit $?
'''
    return subprocess.run(
        ["bash", "-c", bash_snippet], capture_output=True, text=True, timeout=15,
    )


class TestRealPveFixtureVerification:
    """Regression fixture reproducing the EXACT active-ruleset text a
    real first dogfood run (Proxmox VE 9.2.3 / nftables 1.1.3) observed
    via `nft list ruleset` -- proves _verify_firewall_active now accepts
    it (the original bug: it did not, failing exact-content verification
    at Phase 10), and that every required negative case is still
    correctly rejected -- no weakening into loose substring matching.
    """

    def test_exact_real_host_fixture_accepted(self, tmp_path):
        result = _run_firewall_verify_helper(
            tmp_path, active_ruleset_text=_REAL_HOST_FIXTURE, **_REAL_HOST_CONFIG,
        )
        assert result.returncode == 0, result.stderr

    def test_wrong_ha_source_after_canonicalization_rejected(self, tmp_path):
        config = dict(_REAL_HOST_CONFIG, ha_source_cidr="192.168.4.169/32")
        result = _run_firewall_verify_helper(
            tmp_path, active_ruleset_text=_REAL_HOST_FIXTURE, **config,
        )
        assert result.returncode != 0

    def test_wrong_numeric_uid_rejected(self, tmp_path):
        config = dict(_REAL_HOST_CONFIG, hubinetops_uid="1000")
        result = _run_firewall_verify_helper(
            tmp_path, active_ruleset_text=_REAL_HOST_FIXTURE, **config,
        )
        assert result.returncode != 0

    def test_extra_input_rule_rejected(self, tmp_path):
        fixture = _REAL_HOST_FIXTURE.replace(
            "    tcp dport 8787 drop\n",
            "    tcp dport 8787 drop\n    ip saddr 10.0.0.1 tcp dport 22 accept\n",
        )
        result = _run_firewall_verify_helper(
            tmp_path, active_ruleset_text=fixture, **_REAL_HOST_CONFIG,
        )
        assert result.returncode != 0

    def test_extra_output_rule_rejected(self, tmp_path):
        # A duplicated (structurally valid, but not supposed to be there
        # twice) established/related line.
        fixture = _REAL_HOST_FIXTURE.replace(
            "    ct state established,related accept\n",
            "    ct state established,related accept\n    ct state established,related accept\n",
        )
        result = _run_firewall_verify_helper(
            tmp_path, active_ruleset_text=fixture, **_REAL_HOST_CONFIG,
        )
        assert result.returncode != 0

    def test_missing_rule_rejected(self, tmp_path):
        fixture = _REAL_HOST_FIXTURE.replace('    iifname "lo" accept\n', "")
        result = _run_firewall_verify_helper(
            tmp_path, active_ruleset_text=fixture, **_REAL_HOST_CONFIG,
        )
        assert result.returncode != 0

    def test_wrong_ordering_rejected(self, tmp_path):
        # established/related moved AFTER the PVE allow rule instead of
        # before it -- same two rules, wrong order.
        fixture = _REAL_HOST_FIXTURE.replace(
            "    ct state established,related accept\n"
            "    meta skuid 999 ip daddr 192.168.4.249 tcp dport 8006 accept\n",
            "    meta skuid 999 ip daddr 192.168.4.249 tcp dport 8006 accept\n"
            "    ct state established,related accept\n",
        )
        result = _run_firewall_verify_helper(
            tmp_path, active_ruleset_text=fixture, **_REAL_HOST_CONFIG,
        )
        assert result.returncode != 0

    def test_arbitrary_new_hubinetops_egress_rejected(self, tmp_path):
        # A NEW skuid-scoped allow to a destination not on the PVE/DNS
        # allow-list -- must never be silently accepted as "close enough."
        fixture = _REAL_HOST_FIXTURE.replace(
            "    meta skuid 999 drop\n",
            "    meta skuid 999 ip daddr 1.2.3.4 tcp dport 443 accept\n    meta skuid 999 drop\n",
        )
        result = _run_firewall_verify_helper(
            tmp_path, active_ruleset_text=fixture, **_REAL_HOST_CONFIG,
        )
        assert result.returncode != 0


def _run_schema_diagnosis_helper(tmp_path, json_content, field="userid"):
    return _run_json_schema_helper(
        tmp_path, f'_json_list_schema_diagnosis "{{json_file}}" "{field}"',
        json_content=json_content,
    )


class TestJsonListSchemaDiagnosis:
    """Rollback diagnostics fix (sixth pass): a real rollback run logged
    an undifferentiated "did not match the expected JSON shape" message
    for a PVE user-list read-back that a manual read immediately
    afterward showed was genuinely well-formed -- proving nothing about
    THAT one incident's actual cause, since the schema check alone cannot
    distinguish a command hiccup from malformed JSON from a genuinely
    unexpected shape. _json_list_schema_diagnosis exists to let a FUTURE
    occurrence be diagnosed precisely, without loosening the schema or
    dumping the list's own payload.
    """

    def test_empty_file_diagnosed_as_missing_output(self, tmp_path):
        result = _run_schema_diagnosis_helper(tmp_path, "")
        assert result.returncode == 0, result.stderr
        assert result.stdout == "command-output-missing-or-empty"

    def test_malformed_json_diagnosed(self, tmp_path):
        result = _run_schema_diagnosis_helper(tmp_path, "not-valid-json{{{")
        assert result.returncode == 0, result.stderr
        assert result.stdout == "malformed-json"

    def test_non_array_top_level_diagnosed(self, tmp_path):
        result = _run_schema_diagnosis_helper(tmp_path, "{}")
        assert result.returncode == 0, result.stderr
        assert result.stdout == "top-level-not-an-array"

    # -----------------------------------------------------------------
    # Seventh-pass corrective refinement: dogfood #2 hit this exact code
    # path a SECOND time -- the old single "element-not-object-or-
    # missing-<field>-string" catch-all was still too coarse to tell
    # apart three structurally distinct causes. Each is now its own
    # diagnosis string.
    # -----------------------------------------------------------------

    def test_element_not_object_diagnosed(self, tmp_path):
        # A bare-string array element -- not a JSON object at all.
        result = _run_schema_diagnosis_helper(tmp_path, '["hubinetops@pve"]')
        assert result.returncode == 0, result.stderr
        assert result.stdout == "element-not-object"

    def test_required_field_missing_diagnosed(self, tmp_path):
        # An object element, but the required field is simply absent
        # (wrong field name used instead) -- the real dogfood #2 witness.
        result = _run_schema_diagnosis_helper(tmp_path, '[{"user":"hubinetops@pve"}]')
        assert result.returncode == 0, result.stderr
        assert result.stdout == "required-field-missing"

    def test_required_field_null_is_not_string_not_missing(self, tmp_path):
        # Eighth-pass corrective note (P2/P3 finding, independent
        # review): the key IS present with an explicit JSON null value --
        # this must be "required-field-not-string", never
        # "required-field-missing", which must mean the key is actually
        # absent from the object.
        result = _run_schema_diagnosis_helper(tmp_path, '[{"userid":null}]')
        assert result.returncode == 0, result.stderr
        assert result.stdout == "required-field-not-string"

    def test_required_field_not_string_diagnosed(self, tmp_path):
        # The field is present but holds a non-string value.
        result = _run_schema_diagnosis_helper(tmp_path, '[{"userid":12345}]')
        assert result.returncode == 0, result.stderr
        assert result.stdout == "required-field-not-string"

    def test_first_failing_element_determines_diagnosis(self, tmp_path):
        # A valid element followed by an invalid one -- the diagnosis
        # reflects the first element that actually fails, not merely
        # "any" element in the array.
        result = _run_schema_diagnosis_helper(
            tmp_path, '[{"userid":"hubinetops@pve"},{"user":"someone-else"}]',
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == "required-field-missing"

    def test_diagnosis_never_dumps_the_actual_payload(self, tmp_path):
        # Structural diagnosis only -- the field's own (non-secret, but
        # still not appropriate to dump wholesale) value must never
        # appear in the diagnosis output.
        secret_looking_value = "totally-not-a-real-secret-marker-xyz"
        result = _run_schema_diagnosis_helper(
            tmp_path, f'[{{"userid":"{secret_looking_value}","extra":123}}]',
        )
        assert result.returncode == 0, result.stderr
        assert secret_looking_value not in result.stdout

    def test_diagnosis_never_dumps_the_payload_for_each_refined_branch(self, tmp_path):
        # Same guarantee, exercised against each of the three refined
        # branches specifically (not just the pre-existing generic case
        # above), since each now inspects element/field content it did
        # not before.
        secret_looking_value = "totally-not-a-real-secret-marker-xyz"
        for payload in (
            f'["{secret_looking_value}"]',
            f'[{{"user":"{secret_looking_value}"}}]',
            f'[{{"userid":{{"nested":"{secret_looking_value}"}}}}]',
        ):
            result = _run_schema_diagnosis_helper(tmp_path, payload)
            assert result.returncode == 0, result.stderr
            assert secret_looking_value not in result.stdout


def _run_diagnostic_reread_helper(tmp_path, *, list_cmd_body, run_id="test-run-id"):
    """Source ONLY bootstrap-common.sh, define a fake listing command as a
    bash function, then call _diagnostic_ownership_reread directly and
    return the completed subprocess (stderr carries the log_warn output;
    _diagnostic_ownership_reread itself always returns 0). No PVE command,
    no phase function, no orchestration, no real rollback path is ever
    invoked -- this is a pure unit test of the diagnostic-only re-read
    helper in isolation.
    """
    bash_snippet = f'''
source "{(LIB_DIR / "bootstrap-common.sh").as_posix()}"
BOOTSTRAP_RUN_ID="{run_id}"

fake_list_cmd() {{
{list_cmd_body}
}}

_diagnostic_ownership_reread "userid" "hubinetops@pve" "PVE user 'hubinetops@pve'" fake_list_cmd
exit $?
'''
    return subprocess.run(
        ["bash", "-c", bash_snippet], capture_output=True, text=True, timeout=15,
    )


class TestDiagnosticOwnershipReread:
    """Rollback diagnostics fix (seventh pass, Finding B): at most ONE
    additional diagnostic-only re-read of the same listing command after
    an authoritative ownership read was rejected as schema-invalid.
    _diagnostic_ownership_reread itself has NO return-value contract a
    caller could use to authorize deletion -- it only logs structural,
    non-secret facts. These tests exercise it directly, in isolation from
    the full rollback/ownership functions (which are covered end to end
    by the sandbox-only smoke suite).
    """

    def test_always_returns_zero_regardless_of_what_it_finds(self, tmp_path):
        # No return-code contract exists for the caller to misuse as an
        # ownership signal -- exit 0 always, whether the second read
        # succeeds, fails, or is schema-invalid.
        for body in ("return 7", "echo 'not-json{{{'", 'echo \'[{"userid":"hubinetops@pve","comment":"run=test-run-id"}]\''):
            result = _run_diagnostic_reread_helper(tmp_path, list_cmd_body=body)
            assert result.returncode == 0, result.stderr

    def test_second_command_failure_logged_structurally(self, tmp_path):
        result = _run_diagnostic_reread_helper(tmp_path, list_cmd_body="return 9")
        assert "also failed (exit 9)" in result.stderr
        assert "does not change the preserve decision" in result.stderr

    def test_second_schema_invalid_logged_with_diagnosis(self, tmp_path):
        result = _run_diagnostic_reread_helper(
            tmp_path, list_cmd_body="echo '[{\"user\":\"hubinetops@pve\"}]'",
        )
        assert "still schema-invalid" in result.stderr
        assert "diagnosis: required-field-missing" in result.stderr
        assert "does not change the preserve decision" in result.stderr

    def test_second_valid_with_matching_run_marker_logs_true_true(self, tmp_path):
        result = _run_diagnostic_reread_helper(
            tmp_path,
            list_cmd_body='echo \'[{"userid":"hubinetops@pve","comment":"run=test-run-id"}]\'',
        )
        assert "target_present=true" in result.stderr
        assert "run_marker_match=true" in result.stderr
        assert "remains UNPROVEN and PRESERVED" in result.stderr

    def test_second_valid_with_non_matching_run_marker_logs_false(self, tmp_path):
        result = _run_diagnostic_reread_helper(
            tmp_path,
            list_cmd_body='echo \'[{"userid":"hubinetops@pve","comment":"run=some-other-run-id"}]\'',
        )
        assert "target_present=true" in result.stderr
        assert "run_marker_match=false" in result.stderr

    def test_second_valid_but_target_absent_logs_false_false(self, tmp_path):
        result = _run_diagnostic_reread_helper(
            tmp_path,
            list_cmd_body='echo \'[{"userid":"someone-else@pve","comment":"run=test-run-id"}]\'',
        )
        assert "target_present=false" in result.stderr
        assert "run_marker_match=false" in result.stderr

    def test_never_dumps_the_actual_comment_text(self, tmp_path):
        # The comment carries a distinctive marker beyond the plain
        # run=<id> substring the helper checks for -- it must never be
        # echoed into the log, even though it does carry the run marker.
        secret_looking_value = "totally-not-a-real-secret-marker-xyz"
        result = _run_diagnostic_reread_helper(
            tmp_path,
            list_cmd_body=(
                'echo \'[{"userid":"hubinetops@pve",'
                f'"comment":"run=test-run-id extra={secret_looking_value}"}}]\''
            ),
        )
        assert secret_looking_value not in result.stderr
        assert "run_marker_match=true" in result.stderr


def _run_token_ownership_state_helper(tmp_path, *, list_cmd_body, run_id="test-run-id"):
    """Source ONLY bootstrap-common.sh + bootstrap-identity.sh, define a
    fake `pveum` bash function, then call _token_ownership_state directly
    and return the completed subprocess (stdout carries exactly one of
    "owned"/"absent"/"unproven"; stderr carries any log_warn output). No
    PVE command, no phase function, no orchestration, no real rollback
    path is ever invoked.
    """
    bash_snippet = f'''
source "{(LIB_DIR / "bootstrap-common.sh").as_posix()}"
source "{(LIB_DIR / "bootstrap-identity.sh").as_posix()}"
BOOTSTRAP_RUN_ID="{run_id}"

pveum() {{
{list_cmd_body}
}}

_token_ownership_state
exit $?
'''
    return subprocess.run(
        ["bash", "-c", bash_snippet], capture_output=True, text=True, timeout=15,
    )


class TestTokenOwnershipState:
    """P1 fix (eighth pass, independent review of dogfood #2's corrective
    PR): _token_ownership_state must report an explicit tri-state --
    "owned" / "absent" / "unproven" -- never a boolean that collapses
    "genuinely does not exist" (safe to let the parent user be deleted)
    and "could not be verified" (must ALSO block deletion of the parent
    user, since real Proxmox's `pveum user delete` destroys every token
    under the deleted user) into the same indistinguishable signal.
    """

    def test_matching_run_marker_is_owned(self, tmp_path):
        result = _run_token_ownership_state_helper(
            tmp_path,
            list_cmd_body='echo \'[{"tokenid":"r0-readonly","comment":"run=test-run-id"}]\'',
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == "owned"

    def test_genuinely_missing_tokenid_is_absent(self, tmp_path):
        # A schema-valid array that simply does not contain this token.
        result = _run_token_ownership_state_helper(tmp_path, list_cmd_body="echo '[]'")
        assert result.returncode == 0, result.stderr
        assert result.stdout == "absent"

    def test_command_failure_is_unproven_not_absent(self, tmp_path):
        result = _run_token_ownership_state_helper(tmp_path, list_cmd_body="return 3")
        assert result.returncode == 0, result.stderr
        assert result.stdout == "unproven"

    def test_schema_invalid_is_unproven_not_absent(self, tmp_path):
        result = _run_token_ownership_state_helper(tmp_path, list_cmd_body="echo '[1,2,3]'")
        assert result.returncode == 0, result.stderr
        assert result.stdout == "unproven"

    def test_present_with_non_matching_comment_is_unproven_not_absent(self, tmp_path):
        # The token DOES exist (present in a schema-valid array) but its
        # comment belongs to a different run -- this must never be
        # reported as "absent" (there IS something to protect).
        result = _run_token_ownership_state_helper(
            tmp_path,
            list_cmd_body='echo \'[{"tokenid":"r0-readonly","comment":"run=someone-elses-run"}]\'',
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == "unproven"

    def test_present_with_no_comment_at_all_is_unproven_not_absent(self, tmp_path):
        # PVE omits the comment field entirely when unset -- present but
        # commentless must also be "unproven," never "absent."
        result = _run_token_ownership_state_helper(
            tmp_path, list_cmd_body='echo \'[{"tokenid":"r0-readonly"}]\'',
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == "unproven"

    def test_output_is_always_exactly_one_of_the_three_states(self, tmp_path):
        for body in (
            'echo \'[{"tokenid":"r0-readonly","comment":"run=test-run-id"}]\'',
            "echo '[]'",
            "return 9",
            "echo 'not-json{{{'",
        ):
            result = _run_token_ownership_state_helper(tmp_path, list_cmd_body=body)
            assert result.returncode == 0, result.stderr
            assert result.stdout in ("owned", "absent", "unproven")


def _run_parent_user_child_token_state_helper(tmp_path, *, list_cmd_body):
    """Source ONLY bootstrap-common.sh + bootstrap-identity.sh, define a
    fake `pveum` bash function, then call _parent_user_child_token_state
    directly and return the completed subprocess (stdout carries exactly
    one of "empty"/"nonempty"/"unproven"). No PVE command, no phase
    function, no orchestration, no real rollback path is ever invoked.
    """
    bash_snippet = f'''
source "{(LIB_DIR / "bootstrap-common.sh").as_posix()}"
source "{(LIB_DIR / "bootstrap-identity.sh").as_posix()}"
PVE_USER="hubinetops@pve"

pveum() {{
{list_cmd_body}
}}

_parent_user_child_token_state
exit $?
'''
    return subprocess.run(
        ["bash", "-c", bash_snippet], capture_output=True, text=True, timeout=15,
    )


class TestParentUserChildTokenState:
    """P1 fix (tenth pass, independent review -- Codex): "check every
    child token before deleting its parent user." Proving only the
    expected token safe is not sufficient authorization to delete the
    parent user -- a different administrator or a concurrent process
    could have registered an entirely different token under the same
    fixed-name user. _parent_user_child_token_state must report an
    explicit tri-state -- "empty" / "nonempty" / "unproven" -- over a
    FRESH, complete read of the user's ENTIRE token list, never filtered
    to any one expected token.
    """

    def test_genuinely_empty_list_is_empty(self, tmp_path):
        result = _run_parent_user_child_token_state_helper(tmp_path, list_cmd_body="echo '[]'")
        assert result.returncode == 0, result.stderr
        assert result.stdout == "empty"

    def test_one_residual_token_is_nonempty(self, tmp_path):
        result = _run_parent_user_child_token_state_helper(
            tmp_path,
            list_cmd_body='echo \'[{"tokenid":"other-token","comment":"unrelated"}]\'',
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == "nonempty"

    def test_multiple_residual_tokens_are_nonempty(self, tmp_path):
        result = _run_parent_user_child_token_state_helper(
            tmp_path,
            list_cmd_body=(
                'echo \'[{"tokenid":"other-token","comment":"unrelated"},'
                '{"tokenid":"r0-readonly","comment":"run=x"}]\''
            ),
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == "nonempty"

    def test_command_failure_is_unproven_not_empty(self, tmp_path):
        result = _run_parent_user_child_token_state_helper(tmp_path, list_cmd_body="return 7")
        assert result.returncode == 0, result.stderr
        assert result.stdout == "unproven"

    def test_schema_invalid_is_unproven_not_empty(self, tmp_path):
        result = _run_parent_user_child_token_state_helper(tmp_path, list_cmd_body="echo '[1,2,3]'")
        assert result.returncode == 0, result.stderr
        assert result.stdout == "unproven"

    def test_never_dumps_tokenid_or_comment_into_the_log(self, tmp_path):
        secret_looking_value = "totally-not-a-real-secret-marker-xyz"
        result = _run_parent_user_child_token_state_helper(
            tmp_path,
            list_cmd_body=f'echo \'[{{"tokenid":"{secret_looking_value}","comment":"{secret_looking_value}"}}]\'',
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == "nonempty"
        assert secret_looking_value not in result.stderr

    def test_output_is_always_exactly_one_of_the_three_states(self, tmp_path):
        for body in (
            "echo '[]'",
            'echo \'[{"tokenid":"other-token"}]\'',
            "return 9",
            "echo 'not-json{{{'",
        ):
            result = _run_parent_user_child_token_state_helper(tmp_path, list_cmd_body=body)
            assert result.returncode == 0, result.stderr
            assert result.stdout in ("empty", "nonempty", "unproven")


def _run_fake_pveum(fake_env, *args):
    """Invoke the fake `pveum` shim directly (never the real bootstrap
    script, never `_run_full`'s full orchestration) -- a local-safe,
    non-sandbox-gated way to exercise one fake PVE command in isolation.
    Explicit `bash <shim>` rather than executing the shim path directly:
    the shim is a `#!/usr/bin/env bash` script, and direct execution of a
    shebang script by path is not reliably supported by Python's
    subprocess module on a Windows dev machine.
    """
    return subprocess.run(
        ["bash", str(fake_env.bin_dir / "pveum"), *args],
        capture_output=True, text=True, timeout=15, env=fake_env.env,
    )


class TestFakeUserDeleteCascade:
    """Tenth-pass corrective addition (P3 finding, independent review):
    the fake PVE dispatcher's `pveum user delete <user>` handler must
    model real Proxmox's cascade -- deleting the owning user also
    removes every token registered under it -- so a test asserting a
    token survived rollback is genuine proof against the exact hazard
    this repository's rollback_on_failure logic exists to prevent, not
    merely proof that the fake's own `pve_users`/`pve_tokens` dicts
    happen to be unlinked. Exercises the fake directly (bypassing the
    real bootstrap script and rollback logic entirely) to isolate the
    fake's own cascade behavior from the production decision logic
    already covered by TestTokenOwnershipState and the sandbox-gated
    smoke tests.
    """

    def _seed_two_users_each_with_a_token(self, fake_env):
        state = fake_env.state()
        state["pve_users"] = {
            "hubinetops@pve": {"comment": "run=some-run-id"},
            "other@pve": {"comment": "unrelated"},
        }
        state["pve_tokens"] = {
            "hubinetops@pve!r0-readonly": {"comment": "run=some-run-id"},
            "other@pve!sometoken": {"comment": "unrelated"},
        }
        fake_env.state_path.write_text(json.dumps(state), encoding="utf-8")

    def test_user_delete_cascades_to_that_users_own_tokens(self, tmp_path):
        fake_env = build_fake_pve_environment(tmp_path)
        self._seed_two_users_each_with_a_token(fake_env)

        result = _run_fake_pveum(fake_env, "user", "delete", "hubinetops@pve")
        assert result.returncode == 0, result.stderr

        state = fake_env.state()
        assert "hubinetops@pve" not in state["pve_users"]
        assert "hubinetops@pve!r0-readonly" not in state["pve_tokens"]

    def test_user_delete_cascade_does_not_touch_a_different_users_token(self, tmp_path):
        # Scoping check: the cascade must match on the exact "<user>!"
        # prefix, never delete tokens belonging to an unrelated user.
        fake_env = build_fake_pve_environment(tmp_path)
        self._seed_two_users_each_with_a_token(fake_env)

        result = _run_fake_pveum(fake_env, "user", "delete", "hubinetops@pve")
        assert result.returncode == 0, result.stderr

        state = fake_env.state()
        assert "other@pve" in state["pve_users"]
        assert "other@pve!sometoken" in state["pve_tokens"]

    def test_user_delete_of_a_user_with_no_tokens_is_a_no_op_on_pve_tokens(self, tmp_path):
        # Positive control: the cascade logic must not raise/misbehave
        # when the deleted user happens to have no tokens registered.
        fake_env = build_fake_pve_environment(tmp_path)
        state = fake_env.state()
        state["pve_users"] = {"hubinetops@pve": {"comment": "run=some-run-id"}}
        fake_env.state_path.write_text(json.dumps(state), encoding="utf-8")

        result = _run_fake_pveum(fake_env, "user", "delete", "hubinetops@pve")
        assert result.returncode == 0, result.stderr

        state = fake_env.state()
        assert "hubinetops@pve" not in state["pve_users"]
        assert state["pve_tokens"] == {}

    def test_user_delete_cascades_to_all_of_that_users_multiple_tokens(self, tmp_path):
        # Closure-review addition: the real bootstrap only ever creates
        # exactly one token per run, but the cascade implementation itself
        # is a generic loop over every "<user>!"-prefixed entry -- this
        # proves it genuinely removes ALL matches (not merely the first),
        # while still leaving a different user's token untouched.
        fake_env = build_fake_pve_environment(tmp_path)
        state = fake_env.state()
        state["pve_users"] = {
            "userA@pve": {"comment": "run=some-run-id"},
            "userB@pve": {"comment": "unrelated"},
        }
        state["pve_tokens"] = {
            "userA@pve!token1": {"comment": "run=some-run-id"},
            "userA@pve!token2": {"comment": "run=some-run-id"},
            "userB@pve!tokenB": {"comment": "unrelated"},
        }
        fake_env.state_path.write_text(json.dumps(state), encoding="utf-8")

        result = _run_fake_pveum(fake_env, "user", "delete", "userA@pve")
        assert result.returncode == 0, result.stderr

        state = fake_env.state()
        assert "userA@pve" not in state["pve_users"]
        assert "userA@pve!token1" not in state["pve_tokens"]
        assert "userA@pve!token2" not in state["pve_tokens"]
        # A different user's own token, and that user's own object, both
        # survive -- the cascade is scoped exactly to the deleted user.
        assert "userB@pve" in state["pve_users"]
        assert "userB@pve!tokenB" in state["pve_tokens"]


class TestSecurityStatic:
    _ALL_SCRIPTS = ALL_SCRIPTS

    def test_no_eval(self):
        for script in self._ALL_SCRIPTS:
            text = script.read_text(encoding="utf-8")
            assert "eval " not in text and not text.rstrip().endswith("eval"), script

    def test_no_hardcoded_private_or_production_ip(self):
        # RFC 1918/link-local prefixes must never appear as literal
        # addresses in the shipped script -- every network value is a
        # CLI flag/env var, or an RFC 5737 TEST-NET documentation address
        # inside a comment/example, matching this repo's existing
        # convention in deploy/README-0.5-firewall.md.
        forbidden_prefixes = ("192.168.", "10.", "172.16.", "172.17.", "172.18.", "172.19.")
        for script in self._ALL_SCRIPTS:
            text = script.read_text(encoding="utf-8")
            for prefix in forbidden_prefixes:
                assert prefix not in text, f"{script} contains a private-network literal {prefix!r}"

    def test_no_credentials_committed(self):
        for script in self._ALL_SCRIPTS:
            text = script.read_text(encoding="utf-8")
            lowered = text.lower()
            for marker in ("pveapitoken=", "authorization: bearer ey", "-----begin private key-----"):
                assert marker not in lowered

    def test_no_old_04_paths_reintroduced(self):
        for script in self._ALL_SCRIPTS:
            text = script.read_text(encoding="utf-8")
            assert "app.main" not in text
            assert "deploy/pve/hubinet_ops_hostd" not in text
            assert "deploy/managed/" not in text
            assert "OpsService" not in text

    def test_no_arbitrary_command_text_accepted(self):
        # No `bash -c "$SOME_VAR"` / `sh -c "$SOME_VAR"` construction from
        # a caller-supplied variable anywhere in the bootstrap.
        for script in self._ALL_SCRIPTS:
            text = script.read_text(encoding="utf-8")
            assert 'bash -c "$' not in text
            assert "sh -c \"$" not in text

    def test_no_verify_false_anywhere(self):
        # R0 never supports disabling TLS verification. Comment-only
        # lines are stripped first so this does not false-positive on
        # this very sentence appearing in an explanatory comment.
        for script in self._ALL_SCRIPTS:
            code_text = _code_only(script)
            assert "verify=false" not in code_text.lower()
            assert "--insecure" not in code_text
            assert "-k " not in code_text  # curl -k

    def test_no_secret_passed_as_literal_argv_to_a_parser(self):
        # Mandatory Fix 5: the PVE token / R0 bearer token must never be
        # handed to jq/python3/awk/curl as a literal -a/-v/positional
        # argument -- only ever as a file path the parser opens itself.
        # These are the exact anti-patterns the corrective pass removed;
        # a regression reintroducing any of them must fail this test.
        # The precise regression witness: awk/jq/python3 given the token
        # VALUE (not a token_file/perms_file path) as a -v/--arg/positional.
        # Comment-only lines are stripped first (this repository's own
        # explanatory prose deliberately names these exact anti-patterns).
        token_value_patterns = (
            re.compile(r"-v\s+token\s*="),
            re.compile(r"--arg\s+\w*token\w*\s+\"\$\{?pve_token\}?\""),
            re.compile(r'"\$\{pve_token\}"'),
            re.compile(r'"\$\{R0_API_BEARER_TOKEN\}"'),
        )
        for script in self._ALL_SCRIPTS:
            code_text = _code_only(script)
            for pattern in token_value_patterns:
                assert not pattern.search(code_text), f"{script} appears to pass a secret token value as a literal argument: {pattern.pattern}"

    def test_no_curl_bearer_header_with_literal_token_variable(self):
        for script in self._ALL_SCRIPTS:
            text = script.read_text(encoding="utf-8")
            assert "Bearer ${R0_API_BEARER_TOKEN}" not in text
            assert "Bearer ${pve_token}" not in text

    def test_no_hardcoded_vmid_default(self):
        text = BOOTSTRAP_SCRIPT.read_text(encoding="utf-8")
        assert 'VMID="110"' not in text
        assert 'VMID=""' in text, "VMID must default to empty (auto-detect), not a hardcoded value"

    def test_no_implicit_system_tls_default(self):
        text = BOOTSTRAP_SCRIPT.read_text(encoding="utf-8")
        assert 'TLS_TRUST_MODE="system"' not in text
        assert 'TLS_TRUST_MODE=""' in text

    def test_non_git_source_fallback_removed(self):
        for script in self._ALL_SCRIPTS:
            text = script.read_text(encoding="utf-8")
            assert "tar -czf" not in text, f"{script} still contains a non-git tarball fallback"
            assert "is not a git checkout -- falling back" not in text

    def test_no_pveam_update_before_confirm_in_preflight(self):
        code_text = _code_only(LIB_DIR / "bootstrap-preflight.sh")
        assert "pveam update" not in code_text

    def test_python3_or_jq_required_on_host(self):
        text = (LIB_DIR / "bootstrap-preflight.sh").read_text(encoding="utf-8")
        assert "jq" in text and "python3" in text

    def test_exact_permission_set_check_present(self):
        text = (LIB_DIR / "bootstrap-identity.sh").read_text(encoding="utf-8")
        assert "_json_truthy_keys_sorted" in text
        assert "exact-set" in text.lower()

    def test_firewall_port_derived_from_endpoint(self):
        text = (LIB_DIR / "bootstrap-firewall.sh").read_text(encoding="utf-8")
        assert "_endpoint_port" in text
        # No bare hardcoded "8006" used independently as a literal in the
        # rule-generation printf itself (it must flow through pve_port).
        assert 'tcp dport %s accept' in text

    def test_no_legacy_runtime_surface_guard_remains_green(self):
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "tests/test_no_legacy_runtime_surface.py"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# Sanity: the fake-command harness constants used by the sandboxed smoke
# suite are at least importable/consistent from a local-safe context (does
# not execute the target script).
# ---------------------------------------------------------------------------


def test_fake_pve_constants_are_sane():
    assert FAKE_HA_SOURCE_CIDR.endswith("/32")
    assert FAKE_PVE_ENDPOINT.startswith("https://")
    assert FAKE_DISPLAY_NAME
