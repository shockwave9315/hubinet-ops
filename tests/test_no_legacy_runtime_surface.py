"""Permanent anti-regression guard: the 0.5-only repository boundary.

Hubinet Ops 0.5 is a clean-break architecture (AGENTS.md, `docs/architecture/
0.5-foundation.md`: "Git history and old tags are the archive. `main` is not
an archive of obsolete runtime implementations."). The obsolete 0.2.x/0.3.x/
0.4.x implementation, deployment, and Home Assistant presentation surfaces
were retired from the current tree; their historical source remains
recoverable only through Git history/tags.

This file is a narrow, path/import-level guard, not a text-content ban. It
must never fail on a *negative* historical reference inside current 0.5
architecture/operations documentation (e.g. "0.4 migration is unsupported")
-- it only checks that specific legacy *paths* are not tracked again and
that specific legacy *symbols* are not imported by current 0.5 production
modules. Do not extend this file into a blanket text-search ban on "0.2",
"0.3", "0.4", or VMID literals; accepted 0.5 docs legitimately contain such
negative clean-break statements.
"""

from __future__ import annotations

import ast
from pathlib import Path
import subprocess

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _tracked_files() -> set[str]:
    output = subprocess.check_output(
        ["git", "ls-files"], cwd=REPO_ROOT, text=True
    )
    return set(output.splitlines())


# ---------------------------------------------------------------------------
# Prohibited ACTIVE legacy paths -- must never be tracked again.
# ---------------------------------------------------------------------------

_PROHIBITED_EXACT_PATHS = (
    "app/main.py",
    "app/service.py",
    "app/database.py",
    "app/mqtt.py",
    "app/mqtt_budget.py",
    "app/host_control.py",
    "app/executor.py",
    "app/resource_adapters.py",
    "app/ha_entities.py",
    "app/stabilization.py",
    "app/state.py",
    "app/config.py",
    "app/contracts.py",
    "app/security.py",
    "app/time_utils.py",
    "config/config.example.yaml",
    ".env.example",
    "deploy/hubinet-ops.service",
    "scripts/generate_ha_dashboard.py",
)

_PROHIBITED_PATH_PREFIXES = (
    "deploy/managed/",
    "deploy/pve/",
    "home-assistant/",
    "deploy/install-ha-0.",
    "deploy/upgrade-0.",
    "scripts/migrate_config_0_4_",
    "scripts/validate_ha_secrets_0_4_",
    "scripts/validate_rollout_state_0_4_",
    "scripts/validate_managed_profiles",
    "scripts/validate_pve_snapshot_policy",
    "scripts/validate_hermetic_shell_boundary",
)


def test_prohibited_legacy_exact_paths_are_not_tracked() -> None:
    tracked = _tracked_files()
    reintroduced = sorted(tracked & set(_PROHIBITED_EXACT_PATHS))
    assert not reintroduced, (
        "Legacy runtime/config/deploy paths were reintroduced into the "
        f"current 0.5-only tree: {reintroduced}"
    )


def test_prohibited_legacy_path_prefixes_are_not_tracked() -> None:
    tracked = _tracked_files()
    reintroduced = sorted(
        path
        for path in tracked
        if any(path.startswith(prefix) for prefix in _PROHIBITED_PATH_PREFIXES)
    )
    assert not reintroduced, (
        "Legacy deploy/managed, deploy/pve, home-assistant, or versioned "
        f"0.4.x tooling paths were reintroduced: {reintroduced}"
    )


# ---------------------------------------------------------------------------
# Current 0.5 production/runtime modules must never import forbidden legacy
# symbols, even if some legacy module were ever reintroduced under a
# different path.
# ---------------------------------------------------------------------------

_FORBIDDEN_IMPORT_SYMBOLS = (
    "app.main",
    "app.service",
    "app.database",
    "app.mqtt",
    "app.mqtt_budget",
    "app.host_control",
    "app.executor",
    "app.resource_adapters",
    "app.ha_entities",
    "app.stabilization",
    "app.state",
    "app.config",
    "app.contracts",
)

_CURRENT_0_5_PRODUCTION_MODULES = (
    "app/inventory_runtime.py",
    "app/inventory_runtime_config.py",
    "app/inventory_scheduler.py",
    "app/inventory_pve_transport.py",
    "app/inventory/__init__.py",
    "app/inventory/attestation.py",
    "app/inventory/authority.py",
    "app/inventory/canonicalization.py",
    "app/inventory/discovery.py",
    "app/inventory/models.py",
    "app/inventory/provider.py",
    "app/inventory/publication.py",
    "app/inventory/reconciliation.py",
    "app/inventory/store.py",
    "custom_components/hubinet_ops/__init__.py",
    "custom_components/hubinet_ops/api.py",
    "custom_components/hubinet_ops/config_flow.py",
    "custom_components/hubinet_ops/coordinator.py",
    "custom_components/hubinet_ops/diagnostics.py",
    "custom_components/hubinet_ops/entity.py",
    "custom_components/hubinet_ops/sensor.py",
    "custom_components/hubinet_ops/transport_http.py",
)


def _imported_module_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


@pytest.mark.parametrize("rel_path", _CURRENT_0_5_PRODUCTION_MODULES)
def test_current_0_5_module_imports_no_forbidden_legacy_symbol(rel_path: str) -> None:
    full_path = REPO_ROOT / rel_path
    assert full_path.is_file(), f"expected current 0.5 module missing: {rel_path}"
    imported = _imported_module_names(full_path)
    for forbidden in _FORBIDDEN_IMPORT_SYMBOLS:
        offending = {
            name
            for name in imported
            if name == forbidden or name.startswith(forbidden + ".")
        }
        assert not offending, (rel_path, offending)


def test_legacy_ops_service_class_is_not_defined_anywhere_in_current_app() -> None:
    # OpsService is the legacy composition-root service class. It must not
    # be reintroduced as a class definition anywhere under app/ (including a
    # differently-named legacy module), which would defeat the import-name
    # guard above.
    for path in (REPO_ROOT / "app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "OpsService":
                pytest.fail(f"legacy OpsService class reintroduced at {path}")
