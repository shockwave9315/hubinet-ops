"""Local-safe tests for deploy/bootstrap-proxmox-0.5.sh.

Per AGENTS.md's deployment-script sandbox boundary, NO test in this file
ever executes deploy/bootstrap-proxmox-0.5.sh (or sources any deploy/lib/
bootstrap-*.sh phase module) as a real subprocess -- not even against the
hermetic fake-command PATH in tests/_bootstrap_fake_pve.py. That category
of test (the actual functional matrix: preflight, container creation, PVE
identity, TLS, config generation, firewall, rollback, acceptance) lives in
tests/test_bootstrap_proxmox_0_5_smoke.py instead, which only ever runs
inside the Docker-based ephemeral-CI sandbox (see
tests/shell/run_bootstrap_smoke_sandbox.sh) and is a hard pytest skip
everywhere else -- see test_smoke_suite_is_sandbox_gated below, which
proves that structurally rather than by convention.

Everything in this file is either: a pure syntax check (`bash -n`, never
executes script content), a lexical/static text scan of the script source,
or a check that the sandbox infrastructure itself is present and
consistent. tests/_bootstrap_fake_pve.py (the fake command layer used by
the sandboxed smoke suite) is imported here only for its constants/scenario
builders where useful for static assertions -- never to actually run the
target script.
"""

from __future__ import annotations

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
# Static security/content checks
# ---------------------------------------------------------------------------


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
