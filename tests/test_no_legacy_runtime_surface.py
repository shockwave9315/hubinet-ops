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
    # Non-versioned/irregularly-named legacy deploy scripts -- these don't
    # match any _PROHIBITED_PATH_PREFIXES entry (deploy/install-ha-from-
    # pve.sh has no version number at all; deploy/install-ha-dashboard-
    # 0.2.3-from-pve.sh breaks the "deploy/install-ha-0." prefix because of
    # the inserted "dashboard-" segment), so they are listed exactly.
    "deploy/install-agent.sh",
    "deploy/install-ha-from-pve.sh",
    "deploy/install-ha-dashboard-0.2.3-from-pve.sh",
)

_PROHIBITED_PATH_PREFIXES = (
    "deploy/managed/",
    "deploy/pve/",
    "deploy/agent/",
    "home-assistant/",
    # Explicit retired release-line prefixes only -- 0.2.x/0.3.x/0.4.x are
    # the exact retired lines. Deliberately NOT a broad "deploy/install-
    # ha-0." / "deploy/upgrade-0." family prefix: that would also match a
    # future, explicitly-designed 0.5.x installer/upgrader (e.g.
    # "deploy/install-ha-0.5.0-from-pve.sh"), which must not be forbidden
    # merely for sharing the "0." version-namespace fragment. Extend this
    # list one retired line at a time if a future 0.5.x release is itself
    # later retired -- never widen it back to a generic family prefix.
    "deploy/install-ha-0.2.",
    "deploy/install-ha-0.3.",
    "deploy/install-ha-0.4.",
    "deploy/upgrade-0.2.",
    "deploy/upgrade-0.3.",
    "deploy/upgrade-0.4.",
    "scripts/migrate_config_0_4_",
    "scripts/validate_ha_secrets_0_4_",
    "scripts/validate_rollout_state_0_4_",
    "scripts/validate_managed_profiles",
    "scripts/validate_pve_snapshot_policy",
    # NOT "scripts/validate_hermetic_shell_boundary" -- deliberately
    # removed from this list. It was correctly prohibited when it was
    # purely legacy 0.4 shell-smoke tooling; it has since been
    # deliberately restored (verified generic, script-path-argument-only,
    # no hardcoded legacy file list, no 0.4-specific behavior) as current
    # 0.5 tooling backing the R0 Proxmox bootstrap's deployment-script
    # sandbox boundary (deploy/bootstrap-proxmox-0.5.sh,
    # tests/test_bootstrap_proxmox_0_5_smoke.py). This is a reviewed
    # architectural exception, not a silent regression -- do not re-add it
    # here without an equally deliberate reason.
)

# Deliberately narrow prefixes only: each names the exact retired 0.x
# surface, not a whole semantic namespace. A future, explicitly-designed
# 0.5 HA-side deploy artifact (e.g. "deploy/install-ha-0.5.0-from-pve.sh")
# must not be accidentally forbidden merely for living under "deploy/" or
# sharing a version-namespace fragment -- do not widen these to a generic
# "deploy/install-ha", "deploy/upgrade", or "deploy/agent" ban.


def _is_prohibited_legacy_path(path: str) -> bool:
    """Return True if `path` names a retired legacy path this guard forbids.

    Single source of truth for both the exact-path and prefix-family
    matching rules, so the matching *behavior* itself is directly testable
    (see test_guard_classifies_known_legacy_deploy_paths_as_prohibited and
    test_guard_does_not_classify_current_0_5_deploy_paths_as_prohibited)
    independently of whether any matching file happens to be tracked right
    now.
    """
    if path in _PROHIBITED_EXACT_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in _PROHIBITED_PATH_PREFIXES)


def test_prohibited_legacy_exact_paths_are_not_tracked() -> None:
    tracked = _tracked_files()
    reintroduced = sorted(tracked & set(_PROHIBITED_EXACT_PATHS))
    assert not reintroduced, (
        "Legacy runtime/config/deploy paths were reintroduced into the "
        f"current 0.5-only tree: {reintroduced}"
    )


def test_prohibited_legacy_path_prefixes_are_not_tracked() -> None:
    tracked = _tracked_files()
    reintroduced = sorted(path for path in tracked if _is_prohibited_legacy_path(path))
    assert not reintroduced, (
        "Legacy deploy/managed, deploy/pve, deploy/agent, home-assistant, or "
        "retired 0.2.x/0.3.x/0.4.x install/upgrade/tooling paths were "
        f"reintroduced: {reintroduced}"
    )


# ---------------------------------------------------------------------------
# Direct behavioral proof of the matching rule itself -- independent of
# whether any matching file is currently tracked. This is what catches a
# *future* narrowing/widening mistake in the constants above, not just a
# literal reintroduction of a file that happens to be tracked today.
# ---------------------------------------------------------------------------

_KNOWN_LEGACY_DEPLOY_PATHS = (
    "deploy/install-ha-from-pve.sh",
    "deploy/install-ha-dashboard-0.2.3-from-pve.sh",
    "deploy/install-agent.sh",
    "deploy/agent/backup-0.3.0.sh",
    "deploy/agent/restore-0.3.0.sh",
    # Retired 0.2.x/0.3.x/0.4.x release-line installers/upgraders -- these
    # must remain prohibited by the explicit per-line prefixes above.
    "deploy/install-ha-0.2.4-from-pve.sh",
    "deploy/install-ha-0.3.2-from-pve.sh",
    "deploy/install-ha-0.4.2-from-pve.sh",
    "deploy/upgrade-0.2.4-from-pve.sh",
    "deploy/upgrade-0.3.2-from-pve.sh",
    "deploy/upgrade-0.4.2-from-pve.sh",
)

_CURRENT_0_5_DEPLOY_PATHS = (
    "deploy/install-0.5.0-fresh.sh",
    "deploy/hubinet-ops-0.5.service",
    "deploy/README-0.5-firewall.md",
)

# Purely hypothetical future-0.5.x-shaped deploy paths -- neither of these
# installers currently exists in this repository, and their existence is
# not architecturally authorized by this test. They exist only to prove
# the guard's explicit-retired-release-line prefixes do not accidentally
# reserve the entire "0." version namespace forever: a real future 0.5.x
# installer/upgrader, if one is ever designed and added, must not be
# rejected by this guard merely for sharing an "install-ha-0."/
# "upgrade-0." name shape with the retired 0.2/0.3/0.4 lines.
_HYPOTHETICAL_FUTURE_0_5_DEPLOY_PATHS = (
    "deploy/install-ha-0.5.0-from-pve.sh",
    "deploy/upgrade-0.5.0-from-pve.sh",
)


@pytest.mark.parametrize("path", _KNOWN_LEGACY_DEPLOY_PATHS)
def test_guard_classifies_known_legacy_deploy_paths_as_prohibited(path: str) -> None:
    assert _is_prohibited_legacy_path(path), (
        f"guard must classify retired legacy deploy path {path!r} as prohibited"
    )


@pytest.mark.parametrize("path", _CURRENT_0_5_DEPLOY_PATHS)
def test_guard_does_not_classify_current_0_5_deploy_paths_as_prohibited(path: str) -> None:
    assert not _is_prohibited_legacy_path(path), (
        f"guard must not classify current 0.5 deploy path {path!r} as prohibited"
    )


@pytest.mark.parametrize("path", _HYPOTHETICAL_FUTURE_0_5_DEPLOY_PATHS)
def test_guard_does_not_reserve_the_0_5_version_namespace_forever(path: str) -> None:
    assert not _is_prohibited_legacy_path(path), (
        f"guard must not reject a hypothetical future 0.5.x-shaped deploy path "
        f"{path!r} merely for sharing a name shape with the retired 0.2/0.3/0.4 "
        "release lines"
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
    "custom_components/hubinet_ops/const.py",
    "custom_components/hubinet_ops/coordinator.py",
    "custom_components/hubinet_ops/diagnostics.py",
    "custom_components/hubinet_ops/entity.py",
    "custom_components/hubinet_ops/sensor.py",
    "custom_components/hubinet_ops/transport_http.py",
    "custom_components/hubinet_ops/contract/__init__.py",
    "custom_components/hubinet_ops/contract/enums.py",
    "custom_components/hubinet_ops/contract/models.py",
    "custom_components/hubinet_ops/contract/primitives.py",
    "custom_components/hubinet_ops/contract/projections.py",
    "custom_components/hubinet_ops/contract/resource_validation.py",
    "custom_components/hubinet_ops/contract/snapshot_validation.py",
    "custom_components/hubinet_ops/contract/source_validation.py",
    "custom_components/hubinet_ops/contract/transition_validation.py",
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
