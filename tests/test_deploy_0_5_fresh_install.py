"""Fresh production deployment/service.

Covers tests #40 (deployment-smoke portion) and #43 (listen-address/
firewall-pairing documentation check) of
ARCHITECTURE.md.

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

import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SCRIPT = REPO_ROOT / "deploy" / "install-0.5.0-fresh.sh"
SERVICE_UNIT = REPO_ROOT / "deploy" / "hubinet-ops-0.5.service"
FIREWALL_DOC = REPO_ROOT / "deploy" / "README-0.5-firewall.md"


def test_install_script_syntax_is_valid() -> None:
    if shutil.which("bash") is None:
        import pytest

        pytest.skip("bash is not available in this environment")
    result = subprocess.run(
        ["bash", "-n", str(INSTALL_SCRIPT)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# test #40 (deployment-smoke portion) -- never touches an unrelated DB /
# legacy path
# ---------------------------------------------------------------------------


def test_40_install_script_never_copies_the_legacy_04_service_source() -> None:
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    assert "hubinet-ops-0.5.service" in text
    # The legacy repo-relative unit source file must never be referenced
    # as something this installer copies/installs (the *destination*
    # path on the target host, /etc/systemd/system/hubinet-ops.service,
    # is intentionally shared -- see the module docstring /).
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


# The legacy 0.4 installer (`deploy/install-agent.sh`) and service unit
# (`deploy/hubinet-ops.service`) that the current runtime never edited
# have since been retired from the current 0.5-only tree by the repository
# cleanup; the invariant "R0-B's fresh installer never touches the legacy
# 0.4 installer/service source" is now vacuously true (neither file exists)
# and is superseded by the positive checks above, which verify the current
# 0.5 installer's own text/behavior without depending on the legacy files'
# continued existence. Historical source remains available through Git
# history/tags.


# ---------------------------------------------------------------------------
# test #43 -- listen-address / firewall-pairing documentation check
# ---------------------------------------------------------------------------


def test_43_service_binds_all_interfaces_on_the_documented_port() -> None:
    text = SERVICE_UNIT.read_text(encoding="utf-8")
    assert "--host 0.0.0.0" in text
    assert "--port 8787" in text
    assert "app.inventory_runtime:create_app_from_env" in text
    assert "--factory" in text
    # Never revert to a loopback-only default.
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
    enable_index = text.find("systemctl enable --now hubinet-ops")
    assert firewall_index != -1, "install instructions must mention the firewall requirement"
    assert enable_index != -1
    assert firewall_index < enable_index, (
        "the printed instructions must direct the operator to apply the "
        "firewall policy before enabling/starting the service"
    )
    assert "README-0.5-firewall.md" in text


def test_43_install_script_never_starts_or_enables_the_service_itself() -> None:
    # Corrective pass, Finding 4 witness A: the installer must leave the
    # unit installed but not started and not enabled -- an intervening
    # reboot before the firewall is applied must not auto-start it
    # unprotected. Only the printed operator instructions (checked above)
    # may mention systemctl enable/start, never a real invocation in the
    # script body itself.
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    info_block_start = text.index("cat <<INFO")
    script_body = text[:info_block_start]
    assert "systemctl enable" not in script_body
    assert "systemctl start" not in script_body
    assert "systemctl daemon-reload" in script_body


def test_43_firewall_runbook_verifies_before_enabling() -> None:
    text = FIREWALL_DOC.read_text(encoding="utf-8")
    apply_index = text.find("Apply the firewall policy")
    enable_index = text.find("systemctl enable --now hubinet-ops")
    assert apply_index != -1
    assert enable_index != -1
    assert apply_index < enable_index
    normalized = text.replace("*", "")
    assert "does not start or enable it" in normalized


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


# ---------------------------------------------------------------------------
# Corrective pass, P2 Finding 4 -- firewall examples must actually enforce
# the policy they claim, not merely mention the right strings
# ---------------------------------------------------------------------------


def test_finding4_nftables_example_does_not_leave_unrestricted_output_policy() -> None:
    text = FIREWALL_DOC.read_text(encoding="utf-8")
    nft_start = text.index("```nft")
    nft_end = text.index("```", nft_start + 6)
    nft_block = text[nft_start:nft_end]

    # The output chain's own accept policy is fine *only* because egress
    # is separately restricted per-user (skuid) with an explicit trailing
    # drop for that user -- prove both halves of that claim are present,
    # not just the word "policy accept" in isolation.
    assert 'hook output priority 0; policy accept;' in nft_block
    assert 'meta skuid "hubinetops"' in nft_block
    assert 'meta skuid "hubinetops" drop' in nft_block
    assert "tcp dport 8006 accept" in nft_block

    # HA ingress restriction remains explicit.
    assert "tcp dport 8787 accept" in nft_block
    assert "tcp dport 8787 drop" in nft_block


def test_finding4_ufw_example_restricts_other_outbound_traffic() -> None:
    text = FIREWALL_DOC.read_text(encoding="utf-8")
    ufw_start = text.index("```bash", text.index("## Example: `ufw`"))
    ufw_end = text.index("```", ufw_start + 7)
    ufw_block = text[ufw_start:ufw_end]

    assert "ufw default deny outgoing" in ufw_block
    assert "port 8006 proto tcp" in ufw_block  # PVE-only egress explicit
    assert "port 8787 proto tcp" in ufw_block  # HA ingress explicit
    assert "ufw deny 8787/tcp" in ufw_block


def test_finding4_firewall_doc_explains_dns_without_broadening_the_egress_class() -> None:
    text = FIREWALL_DOC.read_text(encoding="utf-8")
    # Any DNS-resolution accommodation must be scoped to the operator's
    # own resolver, never "DNS at large" -- proving this doc never
    # silently expands R0's accepted PVE-only egress contract.
    assert "never DNS at large" in text
    assert "your own local/internal DNS resolver" in text


# ---------------------------------------------------------------------------
# Corrective pass, P2 Finding 5 -- authority filesystem permissions
# ---------------------------------------------------------------------------


def test_finding5_installer_sets_explicit_authority_directory_mode() -> None:
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    assert "install -d -m 0750 -o hubinetops -g hubinetops /var/lib/hubinet-ops" in text


def test_finding5_service_unit_sets_umask_for_0640_authority_files() -> None:
    text = SERVICE_UNIT.read_text(encoding="utf-8")
    assert "UMask=0027" in text


def test_finding5_umask_0027_actually_produces_0640_authority_files(tmp_path: Path) -> None:
    # Behavioral proof, not just a text match: emulate systemd's UMask=0027
    # directive on this process and confirm a freshly created authority DB
    # (and its WAL/SHM siblings) really do end up at mode 0640, never
    # world-readable/writable by default.
    if sys.platform == "win32":
        pytest.skip("POSIX file mode bits are not meaningfully enforced on native Windows")

    from app.inventory import InventoryAuthorityStore

    old_umask = os.umask(0o027)
    try:
        db_path = tmp_path / "authority.db"
        store = InventoryAuthorityStore(db_path)
        try:
            store.backend_instance()
        finally:
            store.close()
    finally:
        os.umask(old_umask)

    mode = stat.S_IMODE(db_path.stat().st_mode)
    assert oct(mode) == oct(0o640), f"authority.db mode was {oct(mode)}, expected 0640"


def test_finding5_secrets_directory_and_files_remain_restricted() -> None:
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    assert "install -d -m 0750 -o root -g hubinetops /etc/hubinet-ops" in text
    assert "chmod 0640 /etc/hubinet-ops/inventory.yaml" in text
    assert "chmod 0640 /etc/hubinet-ops/agent.env" in text
    # Never left at a permissive default.
    assert "chmod 0644 /etc/hubinet-ops" not in text
    assert "chmod 777" not in text
