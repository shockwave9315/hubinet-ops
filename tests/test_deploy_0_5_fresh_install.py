"""WAVE R0-B Family 6 -- fresh 0.5 production deployment/service.

Covers §28 tests #40 (deployment-smoke portion) and #43 (listen-address/
firewall-pairing documentation check) of
docs/architecture/0.5-r0-read-only-runtime-activation.md.

Per AGENTS.md's hermetic test-boundary rules, a production deployment
script may only ever be *executed* inside the repository's CI-only Docker
smoke sandbox on an ephemeral GitHub-hosted runner -- never here. This
file therefore performs exactly the kind of check the mission's Family 6
section explicitly sanctions for this reason: "Text checks are acceptable
for deployment/document contracts where runtime execution would require
host privileges." No command in this file is ever executed against a real
host, real systemd, or a real firewall.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SCRIPT = REPO_ROOT / "deploy" / "install-0.5.0-fresh.sh"
SERVICE_UNIT = REPO_ROOT / "deploy" / "hubinet-ops-0.5.service"
FIREWALL_DOC = REPO_ROOT / "deploy" / "README-0.5-firewall.md"
LEGACY_SERVICE_UNIT = REPO_ROOT / "deploy" / "hubinet-ops.service"


def test_install_script_syntax_is_valid() -> None:
    if shutil.which("bash") is None:
        import pytest

        pytest.skip("bash is not available in this environment")
    result = subprocess.run(
        ["bash", "-n", str(INSTALL_SCRIPT)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# §28 test #40 (deployment-smoke portion) -- never touches an unrelated DB /
# legacy path
# ---------------------------------------------------------------------------


def test_40_install_script_never_copies_the_legacy_04_service_source() -> None:
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    assert "hubinet-ops-0.5.service" in text
    # The legacy repo-relative unit source file must never be referenced
    # as something this installer copies/installs (the *destination*
    # path on the target host, /etc/systemd/system/hubinet-ops.service,
    # is intentionally shared -- see the module docstring / §3).
    assert f"{'${SOURCE_DIR}'}/deploy/hubinet-ops.service" not in text
    assert "install -m 0644 \"${SOURCE_DIR}/deploy/hubinet-ops-0.5.service\"" in text


def test_40_install_script_refuses_a_host_with_an_existing_legacy_install() -> None:
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    assert "/etc/systemd/system/hubinet-ops.service" in text
    assert "/var/lib/hubinet-ops/ops.db" in text
    assert "Refusing to install" in text


def test_40_install_script_never_references_legacy_config_or_ops_db_as_a_dependency() -> None:
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    # "ops.db" is mentioned exactly once, inside the fail-closed
    # pre-existing-install refusal check -- never copied, read, migrated,
    # or used as a fallback anywhere else.
    non_comment_lines_mentioning_ops_db = [
        line
        for line in text.splitlines()
        if "ops.db" in line and not line.strip().startswith("#")
    ]
    assert len(non_comment_lines_mentioning_ops_db) == 1
    assert "LEGACY_DB_PATH" in non_comment_lines_mentioning_ops_db[0]
    assert "config.yaml" not in text
    assert "config/inventory.example.yaml" in text
    assert "config/config.example.yaml" not in text


def test_40_install_script_uses_the_r0_config_and_env_paths() -> None:
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    assert "/etc/hubinet-ops/inventory.yaml" in text
    assert "HUBINET_OPS_R0_CONFIG" in text
    assert "HUBINET_OPS_R0_PVE_TOKEN" in text
    assert "HUBINET_OPS_R0_API_TOKEN" in text
    assert "HUBINET_OPS_DB" not in text  # legacy 0.4 env var name


def test_40_legacy_04_installer_and_service_are_unedited() -> None:
    legacy_installer = REPO_ROOT / "deploy" / "install-agent.sh"
    assert legacy_installer.exists()
    assert LEGACY_SERVICE_UNIT.exists()
    legacy_unit_text = LEGACY_SERVICE_UNIT.read_text(encoding="utf-8")
    assert "app.main:app" in legacy_unit_text
    assert "app.inventory_runtime" not in legacy_unit_text


# ---------------------------------------------------------------------------
# §28 test #43 -- listen-address / firewall-pairing documentation check
# ---------------------------------------------------------------------------


def test_43_service_binds_all_interfaces_on_the_documented_port() -> None:
    text = SERVICE_UNIT.read_text(encoding="utf-8")
    assert "--host 0.0.0.0" in text
    assert "--port 8787" in text
    assert "app.inventory_runtime:create_app_from_env" in text
    assert "--factory" in text
    # Never revert to a loopback-only default (§10/§25).
    assert "127.0.0.1" not in text
    assert "localhost" not in text


def test_43_service_unit_never_depends_on_legacy_paths_or_host_control() -> None:
    text = SERVICE_UNIT.read_text(encoding="utf-8")
    assert "ops.db" not in text
    assert "app.main" not in text
    assert "hostd" not in text.lower()
    assert "mqtt" not in text.lower()


def test_43_firewall_policy_documentation_exists_and_matches_the_bind() -> None:
    assert FIREWALL_DOC.exists()
    text = FIREWALL_DOC.read_text(encoding="utf-8")
    assert "8787" in text
    assert "8006" in text
    assert "ALLOW" in text
    assert "DENY" in text
    # Must document both directions of the required pairing.
    assert "HA host/subnet" in text or "Home Assistant" in text
    assert "PVE endpoint" in text or "Proxmox" in text


def test_43_install_instructions_apply_firewall_before_starting_the_service() -> None:
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    firewall_index = text.find("firewall")
    start_index = text.find("systemctl start hubinet-ops")
    assert firewall_index != -1, "install instructions must mention the firewall requirement"
    assert start_index != -1
    assert firewall_index < start_index, (
        "the printed instructions must direct the operator to apply the "
        "firewall policy before starting the service"
    )
    assert "README-0.5-firewall.md" in text


def test_43_service_unit_hardening_flags_present() -> None:
    text = SERVICE_UNIT.read_text(encoding="utf-8")
    for flag in (
        "NoNewPrivileges=yes",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "RestrictSUIDSGID=yes",
        "LockPersonality=yes",
        "MemoryDenyWriteExecute=yes",
    ):
        assert flag in text
    assert "ReadWritePaths=/var/lib/hubinet-ops" in text
