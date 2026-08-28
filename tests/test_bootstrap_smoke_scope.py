from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "bootstrap-smoke.yml"
HELPER = REPO_ROOT / "scripts" / "bootstrap_smoke_scope.py"

_spec = importlib.util.spec_from_file_location("bootstrap_smoke_scope", HELPER)
assert _spec is not None and _spec.loader is not None
_scope = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_scope)


class BootstrapSmokeScopeTests(unittest.TestCase):
    def _workflow_pull_request_paths(self) -> tuple[str, ...]:
        """Parse the deliberately simple quoted-string paths block without PyYAML."""

        lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
        in_pull_request = False
        in_paths = False
        paths: list[str] = []

        for line in lines:
            if line == "  pull_request:":
                in_pull_request = True
                continue
            if in_pull_request and line == "    paths:":
                in_paths = True
                continue
            if not in_paths:
                continue
            if line.startswith("      - "):
                paths.append(json.loads(line.removeprefix("      - ")))
                continue
            break

        self.assertTrue(paths, "bootstrap-smoke pull_request.paths block was not found")
        return tuple(paths)

    def test_helper_patterns_exactly_match_workflow_paths(self):
        self.assertEqual(self._workflow_pull_request_paths(), _scope.SMOKE_PATHS)

    def test_known_bootstrap_dependencies_require_smoke(self):
        for path in (
            "deploy/bootstrap-proxmox-0.5.sh",
            "deploy/lib/bootstrap-common.sh",
            "deploy/lib/hubinet-ops-bootstrap-accept.py",
            "deploy/lib/hubinet-ops-bootstrap-future-helper.py",
            "deploy/install-0.5.0-fresh.sh",
            "deploy/hubinet-ops-0.5.service",
            "requirements.txt",
            "tests/_bootstrap_fake_pve.py",
            "tests/test_bootstrap_proxmox_0_5_smoke.py",
            "tests/shell/run_bootstrap_smoke_sandbox.sh",
            "tests/shell/nested/future-file",
            "scripts/validate_hermetic_shell_boundary.py",
            "scripts/bootstrap_smoke_scope.py",
            "AGENTS.md",
            ".github/workflows/bootstrap-smoke.yml",
        ):
            with self.subTest(path=path):
                self.assertTrue(_scope.path_requires_smoke(path))

    def test_unrelated_followup_files_do_not_require_smoke(self):
        for path in (
            "docs/product-intent.md",
            "docs/architecture/README.md",
            "README.md",
            "CHANGELOG.md",
            "tests/test_bootstrap_proxmox_0_5.py",
            "tests/test_bootstrap_smoke_scope.py",
            "app/inventory_runtime.py",
        ):
            with self.subTest(path=path):
                self.assertFalse(_scope.path_requires_smoke(path))

    def test_mixed_delta_runs_when_any_one_path_is_relevant(self):
        self.assertTrue(
            _scope.update_requires_smoke(
                ["docs/product-intent.md", "deploy/lib/bootstrap-firewall.sh", "README.md"]
            )
        )
        self.assertFalse(
            _scope.update_requires_smoke(
                ["docs/product-intent.md", "docs/architecture/README.md", "README.md"]
            )
        )

    def test_workflow_uses_synchronize_before_after_and_fail_closed_fallbacks(self):
        text = WORKFLOW.read_text(encoding="utf-8")

        for required in (
            "EVENT_ACTION: ${{ github.event.action }}",
            "EVENT_BEFORE: ${{ github.event.before }}",
            "EVENT_AFTER: ${{ github.event.after }}",
            'git diff --name-only -z "$EVENT_BEFORE" "$EVENT_AFTER"',
            "invalid-synchronize-range",
            "failed-to-fetch-synchronize-range",
            "failed-to-diff-synchronize-range",
            "scope-helper-failed",
            "unexpected-scope-helper-output",
        ):
            with self.subTest(required=required):
                self.assertIn(required, text)

    def test_only_expensive_job_owns_cancel_in_progress_concurrency(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        jobs_prefix, jobs_text = text.split("jobs:\n", 1)
        self.assertNotIn("\nconcurrency:", jobs_prefix)

        scope_text, smoke_text = jobs_text.split("  bootstrap-smoke:\n", 1)
        self.assertNotIn("cancel-in-progress: true", scope_text)
        self.assertIn("needs: scope", smoke_text)
        self.assertIn("if: needs.scope.outputs.run_smoke == 'true'", smoke_text)
        self.assertIn("cancel-in-progress: true", smoke_text)


if __name__ == "__main__":
    unittest.main()
