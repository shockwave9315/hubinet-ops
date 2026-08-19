"""Hermetic tests for deploy/bootstrap-proxmox-0.5.sh (the one-shot Proxmox
bootstrap for the Hubinet Ops 0.5 R0 read-only runtime).

Per AGENTS.md's test-boundary rules, no test here ever invokes a real
`pct`, `pveum`, `pveam`, `pvesh`, `pvesm`, `nft`, `systemctl`, or contacts
a real network/PVE/HA endpoint. tests/_bootstrap_fake_pve.py builds a
temporary PATH containing fake replacements for exactly those command
names; the bootstrap script itself is executed as a real `bash` subprocess
against that PATH, so this file tests the script's actual logic/ordering/
argument construction, not a reimplementation of it.
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
    FAKE_HA_SOURCE_CIDR,
    FAKE_PVE_ENDPOINT,
    FAKE_PVE_ENDPOINT_HOST,
    build_fake_pve_environment,
    build_minimal_source_checkout,
    default_scenario,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_SCRIPT = REPO_ROOT / "deploy" / "bootstrap-proxmox-0.5.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash is not available in this environment"
)


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


def _base_args(**overrides):
    args = {
        "--non-interactive": None,
        "--yes": None,
        "--ha-source": FAKE_HA_SOURCE_CIDR,
        "--pve-endpoint": FAKE_PVE_ENDPOINT,
        "--storage": "local-lxc",
        "--bridge": "vmbr0",
    }
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


@pytest.fixture
def fake_env(tmp_path):
    return build_fake_pve_environment(tmp_path, default_scenario())


@pytest.fixture(scope="session")
def source_checkout(tmp_path_factory):
    # Session-scoped: identical, read-only fixture content for every test
    # in this file. Building it involves a real (but tiny, local-only)
    # `git init`/`commit`, which is unnecessary overhead to repeat once
    # per test.
    return build_minimal_source_checkout(tmp_path_factory.mktemp("bootstrap-src"), REPO_ROOT)


def _run_full(fake_env_obj, tmp_path, source_checkout, *, args=(), scenario_overrides=None):
    if scenario_overrides:
        scenario = default_scenario()
        scenario.update(scenario_overrides)
        fake_env_obj = build_fake_pve_environment(tmp_path, scenario)
    return _run(fake_env_obj.env, _base_args() + list(args), source_dir=source_checkout)


# ---------------------------------------------------------------------------
# Static syntax check
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "script",
    [
        "deploy/bootstrap-proxmox-0.5.sh",
        "deploy/lib/bootstrap-common.sh",
        "deploy/lib/bootstrap-preflight.sh",
        "deploy/lib/bootstrap-container.sh",
        "deploy/lib/bootstrap-identity.sh",
        "deploy/lib/bootstrap-deploy.sh",
        "deploy/lib/bootstrap-firewall.sh",
        "deploy/lib/bootstrap-finish.sh",
    ],
)
def test_syntax_is_valid(script):
    result = subprocess.run(
        ["bash", "-n", str(REPO_ROOT / script)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


class TestPreflight:
    def test_non_root_rejected(self, fake_env, source_checkout):
        env = dict(fake_env.env)
        env.pop("BOOTSTRAP_TEST_MODE", None)  # force the real EUID check
        result = _run(env, _base_args(), source_dir=source_checkout)
        assert result.returncode != 0
        assert "must run as root" in result.stderr

    def test_missing_pve_command_rejected(self, fake_env, source_checkout):
        env = dict(fake_env.env)
        # Remove the fake bin dir from PATH so 'pct' resolves to nothing.
        env["PATH"] = env["PATH"].split(str(fake_env.bin_dir) + __import__("os").pathsep, 1)[-1]
        result = _run(env, _base_args(), source_dir=source_checkout)
        assert result.returncode != 0
        assert "not found" in result.stderr

    def test_existing_vmid_rejected_without_any_destroy(self, fake_env, source_checkout):
        fake_env.state_path.write_text(
            json.dumps({"vmids": {"110": {"started": False}}, "pve_users": [], "pve_roles": [], "pve_tokens": []})
        )
        result = _run(fake_env.env, _base_args(), source_dir=source_checkout)
        assert result.returncode != 0
        assert "already exists" in result.stderr
        assert "pct create" not in fake_env.log_path.read_text() if fake_env.log_path.exists() else True
        for line in fake_env.log_lines():
            assert not line.startswith("pct create")
            assert not line.startswith("pct destroy")

    def test_invalid_bridge_rejected(self, fake_env, source_checkout):
        result = _run(
            fake_env.env, _base_args(**{"--bridge": "vmbr99"}), source_dir=source_checkout
        )
        assert result.returncode != 0
        assert "vmbr99" in result.stderr
        assert "does not exist" in result.stderr

    def test_invalid_storage_rejected(self, fake_env, source_checkout):
        result = _run(
            fake_env.env, _base_args(**{"--storage": "bogus-storage"}), source_dir=source_checkout
        )
        assert result.returncode != 0
        assert "bogus-storage" in result.stderr

    def test_missing_template_behavior_falls_back_to_download(self, tmp_path, source_checkout):
        scenario = default_scenario()
        scenario["local_templates"] = []
        env = build_fake_pve_environment(tmp_path, scenario)
        result = _run(env.env, _base_args(), source_dir=source_checkout)
        assert result.returncode == 0, result.stderr
        assert any("pveam" in line and "download" in line for line in env.log_lines())

    def test_missing_template_entirely_fails_closed(self, tmp_path, source_checkout):
        scenario = default_scenario()
        scenario["local_templates"] = []
        scenario["available_templates"] = []
        env = build_fake_pve_environment(tmp_path, scenario)
        result = _run(env.env, _base_args(), source_dir=source_checkout)
        assert result.returncode != 0
        assert "template" in result.stderr.lower()

    def test_unsupported_template_name_rejected(self, fake_env, source_checkout):
        result = _run(
            fake_env.env,
            _base_args(**{"--template": "local:vztmpl/ubuntu-24.04-standard_amd64.tar.zst"}),
            source_dir=source_checkout,
        )
        assert result.returncode != 0
        assert "does not look like a supported Debian 13" in result.stderr

    def test_invalid_ha_cidr_rejected(self, fake_env, source_checkout):
        result = _run(
            fake_env.env, _base_args(**{"--ha-source": "not-a-cidr"}), source_dir=source_checkout
        )
        assert result.returncode != 0
        assert "not a valid IPv4 CIDR" in result.stderr

    def test_invalid_vmid_rejected(self, fake_env, source_checkout):
        result = _run(fake_env.env, _base_args(**{"--vmid": "99"}), source_dir=source_checkout)
        assert result.returncode != 0
        assert "not a valid Proxmox VMID" in result.stderr

    def test_static_network_requires_ip_and_gateway(self, fake_env, source_checkout):
        result = _run(
            fake_env.env,
            _base_args(**{"--network": "static"}),
            source_dir=source_checkout,
        )
        assert result.returncode != 0
        assert "--ip" in result.stderr

    def test_insufficient_storage_space_rejected(self, tmp_path, source_checkout):
        scenario = default_scenario()
        scenario["storage_available_bytes"] = 1024  # 1 KiB, far below 8 GiB rootfs
        env = build_fake_pve_environment(tmp_path, scenario)
        result = _run(env.env, _base_args(), source_dir=source_checkout)
        assert result.returncode != 0
        assert "free space" in result.stderr


# ---------------------------------------------------------------------------
# Container creation
# ---------------------------------------------------------------------------


class TestContainerCreation:
    def test_pct_create_arguments(self, fake_env, source_checkout):
        result = _run(fake_env.env, _base_args(), source_dir=source_checkout)
        assert result.returncode == 0, result.stderr
        create_lines = [line for line in fake_env.log_lines() if line.startswith("pct create")]
        assert len(create_lines) == 1
        line = create_lines[0]
        assert "--unprivileged 1" in line
        assert "--onboot 0" in line
        assert "net0 name=eth0,bridge=vmbr0,firewall=1,ip=dhcp" in line
        assert "--cores 1" in line
        assert "--memory 1024" in line
        assert "--swap 512" in line
        assert "local-lxc:8" in line

    def test_static_network_configuration(self, fake_env, source_checkout):
        result = _run(
            fake_env.env,
            _base_args(**{"--network": "static", "--ip": "192.0.2.5/24", "--gateway": "192.0.2.1"}),
            source_dir=source_checkout,
        )
        assert result.returncode == 0, result.stderr
        create_lines = [line for line in fake_env.log_lines() if line.startswith("pct create")]
        assert "ip=192.0.2.5/24,gw=192.0.2.1" in create_lines[0]

    def test_debian13_nesting_enabled(self, fake_env, source_checkout):
        result = _run(fake_env.env, _base_args(), source_dir=source_checkout)
        assert result.returncode == 0, result.stderr
        set_lines = [line for line in fake_env.log_lines() if line.startswith("pct set 110 --features")]
        assert any("nesting=1" in line for line in set_lines)

    def test_non_matching_local_template_is_skipped_not_selected(self, tmp_path, source_checkout):
        # A present-but-wrong-shaped local template must never be silently
        # selected -- distinct from test_missing_template_entirely_fails_closed
        # (no templates anywhere), this proves the local-template scan
        # correctly filters out a non-matching entry and still finds the
        # genuinely available Debian 13 template via download, rather than
        # picking the alpine one merely because *something* exists locally.
        scenario = default_scenario()
        scenario["local_templates"] = ["local:vztmpl/alpine-3.20-default_20240607_amd64.tar.xz"]
        env = build_fake_pve_environment(tmp_path, scenario)
        result = _run(env.env, _base_args(), source_dir=source_checkout)
        assert result.returncode == 0, result.stderr
        assert "alpine" not in result.stderr.lower()
        create_lines = [line for line in env.log_lines() if line.startswith("pct create")]
        assert "debian-13-standard" in create_lines[0]
        assert "alpine" not in create_lines[0]

    def test_no_debian13_template_anywhere_is_rejected(self, tmp_path, source_checkout):
        scenario = default_scenario()
        scenario["local_templates"] = ["local:vztmpl/alpine-3.20-default_20240607_amd64.tar.xz"]
        scenario["available_templates"] = []
        env = build_fake_pve_environment(tmp_path, scenario)
        result = _run(env.env, _base_args(), source_dir=source_checkout)
        assert result.returncode != 0
        assert "template" in result.stderr.lower()

    def test_no_unrelated_lxc_features_set(self, fake_env, source_checkout):
        result = _run(fake_env.env, _base_args(), source_dir=source_checkout)
        assert result.returncode == 0, result.stderr
        set_lines = [line for line in fake_env.log_lines() if line.startswith("pct set 110 --features")]
        for line in set_lines:
            features = line.split("--features", 1)[1].strip()
            assert set(features.split(",")) <= {"nesting=1"}


# ---------------------------------------------------------------------------
# PVE identity
# ---------------------------------------------------------------------------


class TestPveIdentity:
    def test_exact_role_privileges(self, fake_env, source_checkout):
        result = _run(fake_env.env, _base_args(), source_dir=source_checkout)
        assert result.returncode == 0, result.stderr
        role_lines = [line for line in fake_env.log_lines() if line.startswith("pveum role add")]
        assert len(role_lines) == 1
        assert "--privs Sys.Audit,VM.Audit" in role_lines[0]

    def test_privsep_token_created(self, fake_env, source_checkout):
        result = _run(fake_env.env, _base_args(), source_dir=source_checkout)
        assert result.returncode == 0, result.stderr
        token_lines = [line for line in fake_env.log_lines() if line.startswith("pveum user token add")]
        assert len(token_lines) == 1
        assert "--privsep 1" in token_lines[0]
        assert "r0-readonly" in token_lines[0]

    def test_separate_token_acl_grant(self, fake_env, source_checkout):
        result = _run(fake_env.env, _base_args(), source_dir=source_checkout)
        assert result.returncode == 0, result.stderr
        acl_lines = [line for line in fake_env.log_lines() if line.startswith("pveum acl modify")]
        assert any("--users hubinetops@pve" in line for line in acl_lines)
        assert any("--tokens hubinetops@pve!r0-readonly" in line for line in acl_lines)

    def test_conflict_refusal_existing_user(self, tmp_path, source_checkout):
        scenario = default_scenario()
        env = build_fake_pve_environment(tmp_path, scenario)
        env.state_path.write_text(
            json.dumps({"vmids": {}, "pve_users": ["hubinetops@pve"], "pve_roles": [], "pve_tokens": []})
        )
        result = _run(env.env, _base_args(), source_dir=source_checkout)
        assert result.returncode != 0
        assert "already exists" in result.stderr
        assert "hubinetops@pve" in result.stderr
        assert not any(line.startswith("pveum user add") for line in env.log_lines())

    def test_conflict_refusal_existing_role(self, tmp_path, source_checkout):
        scenario = default_scenario()
        env = build_fake_pve_environment(tmp_path, scenario)
        env.state_path.write_text(
            json.dumps({"vmids": {}, "pve_users": [], "pve_roles": ["HubinetOpsR0Auditor"], "pve_tokens": []})
        )
        result = _run(env.env, _base_args(), source_dir=source_checkout)
        assert result.returncode != 0
        assert "HubinetOpsR0Auditor" in result.stderr
        assert "already exists" in result.stderr

    def test_missing_required_privilege_fails_verification(self, tmp_path, source_checkout):
        scenario = default_scenario()
        scenario["token_permissions"] = {"Sys.Audit": 1}  # VM.Audit missing
        env = build_fake_pve_environment(tmp_path, scenario)
        result = _run(env.env, _base_args(), source_dir=source_checkout)
        assert result.returncode != 0
        assert "VM.Audit" in result.stderr

    def test_extra_mutation_privilege_fails_verification(self, tmp_path, source_checkout):
        scenario = default_scenario()
        scenario["token_permissions"] = {"Sys.Audit": 1, "VM.Audit": 1, "VM.Config.Disk": 1}
        env = build_fake_pve_environment(tmp_path, scenario)
        result = _run(env.env, _base_args(), source_dir=source_checkout)
        assert result.returncode != 0
        assert "mutation-shaped privilege" in result.stderr

    def test_token_secret_never_appears_in_stdout_or_stderr(self, fake_env, source_checkout):
        result = _run(fake_env.env, _base_args(), source_dir=source_checkout)
        assert result.returncode == 0, result.stderr
        secret = "00000000-0000-0000-0000-000000000000"
        assert secret not in result.stdout
        assert secret not in result.stderr

    def test_token_secret_never_appears_in_fake_command_log(self, fake_env, source_checkout):
        result = _run(fake_env.env, _base_args(), source_dir=source_checkout)
        assert result.returncode == 0, result.stderr
        secret = "00000000-0000-0000-0000-000000000000"
        log_text = fake_env.log_path.read_text() if fake_env.log_path.exists() else ""
        assert secret not in log_text

    def test_rollback_removes_pve_identity_on_later_failure(self, tmp_path, source_checkout):
        scenario = default_scenario()
        scenario["fail"] = ["backend_health"]
        env = build_fake_pve_environment(tmp_path, scenario)
        result = _run(env.env, _base_args(), source_dir=source_checkout)
        assert result.returncode != 0
        log_lines = env.log_lines()
        assert any(line.startswith("pveum user token remove") for line in log_lines)
        assert any(line.startswith("pveum role delete") for line in log_lines)
        assert any(line.startswith("pveum user delete") for line in log_lines)


# ---------------------------------------------------------------------------
# TLS trust
# ---------------------------------------------------------------------------


class TestTls:
    def test_ca_bundle_deployed_when_available(self, tmp_path, fake_env, source_checkout):
        fake_pve_ca = tmp_path / "pve-root-ca.pem"
        fake_pve_ca.write_text("-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n")
        result = _run(
            fake_env.env,
            _base_args(**{"--pve-ca-path": str(fake_pve_ca)}),
            source_dir=source_checkout,
        )
        assert result.returncode == 0, result.stderr
        inventory = fake_env.ct_file_text("110", "/etc/hubinet-ops/inventory.yaml")
        assert 'ca_bundle_path: "/etc/hubinet-ops/pve-ca.pem"' in inventory
        assert "verify: true" in inventory

    def test_no_verify_false_path_exists_anywhere_in_the_script(self):
        # Checks executable (non-comment) lines only -- the scripts'
        # own explanatory comments legitimately *mention* "verify=false"
        # while explaining that it is never done; a blanket whole-file
        # substring scan would false-positive on exactly that prose.
        for script in (BOOTSTRAP_SCRIPT, *sorted((REPO_ROOT / "deploy" / "lib").glob("*.sh"))):
            code_lines = [
                line for line in script.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]
            code_text = "\n".join(code_lines).lower()
            assert "verify: false" not in code_text
            assert "verify=false" not in code_text.replace(" ", "")
            assert "--insecure" not in code_text
            assert "-k http" not in code_text

    def test_missing_explicit_ca_path_fails_closed(self, fake_env, source_checkout):
        result = _run(
            fake_env.env,
            _base_args(**{"--pve-ca-path": "/nonexistent/ca.pem"}),
            source_dir=source_checkout,
        )
        assert result.returncode != 0
        assert "does not exist" in result.stderr

    def test_generated_inventory_always_sets_verify_true(self, fake_env, source_checkout):
        result = _run(fake_env.env, _base_args(), source_dir=source_checkout)
        assert result.returncode == 0, result.stderr
        inventory = fake_env.ct_file_text("110", "/etc/hubinet-ops/inventory.yaml")
        assert "verify: true" in inventory
        assert "verify: false" not in inventory


# ---------------------------------------------------------------------------
# Config generation
# ---------------------------------------------------------------------------


class TestConfigGeneration:
    def test_inventory_is_source_centric_only(self, fake_env, source_checkout):
        result = _run(fake_env.env, _base_args(), source_dir=source_checkout)
        assert result.returncode == 0, result.stderr
        inventory = fake_env.ct_file_text("110", "/etc/hubinet-ops/inventory.yaml")
        # Checks executable (non-comment) YAML lines only -- the generated
        # file's own explanatory header comment legitimately *mentions*
        # "vmid" while explaining that none exists; a blanket whole-file
        # substring scan would false-positive on exactly that prose.
        code_lines = "\n".join(
            line for line in inventory.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ).lower()
        for forbidden in (
            "vmid",
            "vmids",
            "containers:",
            "resources:",
            "managed-vmids",
            "allowed-vmids",
            "lifecycle-vmids",
        ):
            assert forbidden not in code_lines
        assert "source:" in inventory
        assert "provider_kind: proxmox_ve" in inventory

    def test_pve_token_only_in_agent_env_never_in_inventory_yaml(self, fake_env, source_checkout):
        result = _run(fake_env.env, _base_args(), source_dir=source_checkout)
        assert result.returncode == 0, result.stderr
        inventory = fake_env.ct_file_text("110", "/etc/hubinet-ops/inventory.yaml")
        agent_env = fake_env.ct_file_text("110", "/etc/hubinet-ops/agent.env")
        secret = "00000000-0000-0000-0000-000000000000"
        assert secret not in inventory
        assert secret in agent_env
        assert "HUBINET_OPS_R0_PVE_TOKEN=hubinetops@pve!r0-readonly=" + secret in agent_env

    def test_credential_reference_is_opaque_not_the_secret(self, fake_env, source_checkout):
        result = _run(fake_env.env, _base_args(), source_dir=source_checkout)
        assert result.returncode == 0, result.stderr
        inventory = fake_env.ct_file_text("110", "/etc/hubinet-ops/inventory.yaml")
        assert 'credential_reference: "secret://' in inventory

    def test_generated_files_pushed_with_restrictive_ownership(self, fake_env, source_checkout):
        result = _run(fake_env.env, _base_args(), source_dir=source_checkout)
        assert result.returncode == 0, result.stderr
        log = fake_env.log_lines()
        assert any(
            "chown root:hubinetops /etc/hubinet-ops/inventory.yaml" in line for line in log
        )
        assert any("chmod 0640 /etc/hubinet-ops/inventory.yaml" in line for line in log)
        assert any("chown root:hubinetops /etc/hubinet-ops/agent.env" in line for line in log)
        assert any("chmod 0640 /etc/hubinet-ops/agent.env" in line for line in log)

    def test_existing_r0_api_bearer_token_from_installer_is_preserved(self, fake_env, source_checkout):
        result = _run(fake_env.env, _base_args(), source_dir=source_checkout)
        assert result.returncode == 0, result.stderr
        agent_env = fake_env.ct_file_text("110", "/etc/hubinet-ops/agent.env")
        assert f"HUBINET_OPS_R0_API_TOKEN={'f' * 64}" in agent_env


# ---------------------------------------------------------------------------
# Firewall
# ---------------------------------------------------------------------------


class TestFirewall:
    def test_exact_ha_source_allowed_to_8787(self, fake_env, source_checkout):
        result = _run(fake_env.env, _base_args(), source_dir=source_checkout)
        assert result.returncode == 0, result.stderr
        ruleset = fake_env.ct_file_text("110", "/etc/nftables.conf")
        assert f"ip saddr {FAKE_HA_SOURCE_CIDR} tcp dport 8787 accept" in ruleset

    def test_default_deny_for_other_8787_ingress(self, fake_env, source_checkout):
        result = _run(fake_env.env, _base_args(), source_dir=source_checkout)
        assert result.returncode == 0, result.stderr
        ruleset = fake_env.ct_file_text("110", "/etc/nftables.conf")
        assert "tcp dport 8787 drop" in ruleset

    def test_hubinetops_egress_confined_to_pve_endpoint_8006(self, fake_env, source_checkout):
        result = _run(fake_env.env, _base_args(), source_dir=source_checkout)
        assert result.returncode == 0, result.stderr
        ruleset = fake_env.ct_file_text("110", "/etc/nftables.conf")
        assert f'meta skuid "hubinetops" ip daddr {FAKE_PVE_ENDPOINT_HOST} tcp dport 8006 accept' in ruleset
        assert 'meta skuid "hubinetops" drop' in ruleset

    def test_no_dns_rule_when_endpoint_is_a_literal_ip(self, fake_env, source_checkout):
        result = _run(fake_env.env, _base_args(), source_dir=source_checkout)
        assert result.returncode == 0, result.stderr
        ruleset = fake_env.ct_file_text("110", "/etc/nftables.conf")
        assert "udp dport 53" not in ruleset

    def test_dns_resolver_required_when_endpoint_is_a_hostname(self, fake_env, source_checkout):
        # Fails at phase 10, after the CT already exists in this fake_env's
        # state -- deliberately not reused by the positive case below (a
        # second run against the same environment would then hit "VMID
        # already exists" in phase 1, which is a different, already-covered
        # concern, not what this test is about).
        result = _run(
            fake_env.env,
            _base_args(**{"--pve-endpoint": "https://pve.example.internal:8006"}),
            source_dir=source_checkout,
        )
        assert result.returncode != 0
        assert "--dns-resolver" in result.stderr

    def test_dns_rule_scoped_to_configured_resolver_when_endpoint_is_a_hostname(
        self, tmp_path, source_checkout
    ):
        env = build_fake_pve_environment(tmp_path, default_scenario())
        result = _run(
            env.env,
            _base_args(**{"--pve-endpoint": "https://pve.example.internal:8006", "--dns-resolver": "192.0.2.53"}),
            source_dir=source_checkout,
        )
        assert result.returncode == 0, result.stderr
        ruleset = env.ct_file_text("110", "/etc/nftables.conf")
        assert 'meta skuid "hubinetops" ip daddr 192.0.2.53 udp dport 53 accept' in ruleset

    def test_syntax_validated_before_activation(self, tmp_path, source_checkout):
        scenario = default_scenario()
        scenario["fail"] = ["nft_syntax"]
        env = build_fake_pve_environment(tmp_path, scenario)
        result = _run(env.env, _base_args(), source_dir=source_checkout)
        assert result.returncode != 0
        assert "syntax validation" in result.stderr
        assert not any(
            line.startswith("systemctl") and "restart" in line and "nftables" in line
            for line in env.log_lines()
        )

    def test_service_cannot_start_before_firewall_succeeds(self, tmp_path, source_checkout):
        scenario = default_scenario()
        scenario["fail"] = ["nft_activate"]
        env = build_fake_pve_environment(tmp_path, scenario)
        result = _run(env.env, _base_args(), source_dir=source_checkout)
        assert result.returncode != 0
        assert not any(
            "systemctl" in line and "enable" in line and "--now" in line and "hubinet-ops" in line
            for line in env.log_lines()
        )


# ---------------------------------------------------------------------------
# Ordering / rollback / onboot semantics
# ---------------------------------------------------------------------------


class TestOrderingAndRollback:
    def test_full_phase_ordering_on_success(self, fake_env, source_checkout):
        result = _run(fake_env.env, _base_args(), source_dir=source_checkout)
        assert result.returncode == 0, result.stderr
        phases = [
            line for line in result.stderr.splitlines() if line.strip().startswith("[hubinet-ops-bootstrap] === Phase")
        ]
        numbers = [int(line.split("Phase")[1].split(":")[0].strip()) for line in phases]
        assert numbers == sorted(numbers)
        assert numbers == list(range(1, 14))

    def test_onboot_enabled_only_at_the_very_end(self, fake_env, source_checkout):
        result = _run(fake_env.env, _base_args(), source_dir=source_checkout)
        assert result.returncode == 0, result.stderr
        log = fake_env.log_lines()
        onboot_calls = [i for i, line in enumerate(log) if "--onboot 1" in line]
        assert len(onboot_calls) == 1
        service_enable_calls = [
            i for i, line in enumerate(log) if line.startswith("pct exec 110 -- systemctl enable --now hubinet-ops")
        ]
        assert len(service_enable_calls) == 1
        assert onboot_calls[0] > service_enable_calls[0]

    def test_firewall_activation_happens_before_service_enable(self, fake_env, source_checkout):
        result = _run(fake_env.env, _base_args(), source_dir=source_checkout)
        assert result.returncode == 0, result.stderr
        log = fake_env.log_lines()
        firewall_idx = next(
            i for i, line in enumerate(log) if "systemctl restart nftables" in line
        )
        service_idx = next(
            i for i, line in enumerate(log) if "systemctl enable --now hubinet-ops" in line
        )
        assert firewall_idx < service_idx

    def test_failure_cannot_leave_service_enabled_or_ct_onboot_enabled(self, tmp_path, source_checkout):
        scenario = default_scenario()
        scenario["fail"] = ["backend_snapshot"]
        env = build_fake_pve_environment(tmp_path, scenario)
        result = _run(env.env, _base_args(), source_dir=source_checkout)
        assert result.returncode != 0

        state = json.loads(env.state_path.read_text())
        entry = state["vmids"]["110"]
        assert entry.get("onboot", "0") == "0"
        assert entry.get("service_enabled", False) is False

    def test_preserve_on_failure_is_the_default(self, tmp_path, source_checkout):
        scenario = default_scenario()
        scenario["fail"] = ["backend_health"]
        env = build_fake_pve_environment(tmp_path, scenario)
        result = _run(env.env, _base_args(), source_dir=source_checkout)
        assert result.returncode != 0
        assert not any(line.startswith("pct destroy") for line in env.log_lines())
        assert "preserving container" in result.stderr

        state = json.loads(env.state_path.read_text())
        assert "110" in state["vmids"]  # CT itself still exists

    def test_cleanup_on_failure_flag_destroys_the_container(self, tmp_path, source_checkout):
        scenario = default_scenario()
        scenario["fail"] = ["backend_health"]
        env = build_fake_pve_environment(tmp_path, scenario)
        result = _run(env.env, _base_args() + ["--cleanup-on-failure"], source_dir=source_checkout)
        assert result.returncode != 0
        assert any(line.startswith("pct destroy") or "pct stop" in line for line in env.log_lines())

        state = json.loads(env.state_path.read_text())
        assert "110" not in state["vmids"]

    def test_rollback_never_touches_a_preexisting_vmid(self, tmp_path, source_checkout):
        # Preexisting VMID -> preflight stops before any creation, so
        # rollback must never issue pct destroy/stop for it.
        scenario = default_scenario()
        env = build_fake_pve_environment(tmp_path, scenario)
        env.state_path.write_text(
            json.dumps({"vmids": {"110": {"started": False}}, "pve_users": [], "pve_roles": [], "pve_tokens": []})
        )
        result = _run(env.env, _base_args() + ["--cleanup-on-failure"], source_dir=source_checkout)
        assert result.returncode != 0
        assert not any(line.startswith("pct destroy") for line in env.log_lines())
        assert not any(line.startswith("pct stop") for line in env.log_lines())


# ---------------------------------------------------------------------------
# Acceptance
# ---------------------------------------------------------------------------


class TestAcceptance:
    def test_mocked_healthy_success_path(self, fake_env, source_checkout):
        result = _run(fake_env.env, _base_args(), source_dir=source_checkout)
        assert result.returncode == 0, result.stderr
        assert "Hubinet Ops 0.5 R0 bootstrap: PASS" in result.stdout

    def test_mocked_discovery_failure(self, tmp_path, source_checkout):
        scenario = default_scenario()
        scenario["fail"] = ["backend_snapshot"]
        env = build_fake_pve_environment(tmp_path, scenario)
        result = _run(env.env, _base_args(), source_dir=source_checkout)
        assert result.returncode != 0
        assert "snapshot" in result.stderr.lower()

    def test_mocked_backend_health_failure(self, tmp_path, source_checkout):
        scenario = default_scenario()
        scenario["fail"] = ["backend_health"]
        env = build_fake_pve_environment(tmp_path, scenario)
        result = _run(env.env, _base_args(), source_dir=source_checkout)
        assert result.returncode != 0
        assert "health" in result.stderr.lower()

    def test_failed_systemd_units_block_acceptance(self, tmp_path, source_checkout):
        scenario = default_scenario()
        scenario["failed_units"] = ["dev-mqueue.mount", "run-lock.mount", "tmp.mount"]
        env = build_fake_pve_environment(tmp_path, scenario)
        result = _run(env.env, _base_args(), source_dir=source_checkout)
        assert result.returncode != 0
        assert "dev-mqueue.mount" in result.stderr

    def test_legacy_ops_db_presence_blocks_acceptance(self, tmp_path, source_checkout):
        scenario = default_scenario()
        scenario["legacy_present"] = {"ops_db": True}
        env = build_fake_pve_environment(tmp_path, scenario)
        result = _run(env.env, _base_args(), source_dir=source_checkout)
        assert result.returncode != 0
        assert "ops.db" in result.stderr

    def test_legacy_hostd_presence_blocks_acceptance(self, tmp_path, source_checkout):
        scenario = default_scenario()
        scenario["legacy_present"] = {"hostd": True}
        env = build_fake_pve_environment(tmp_path, scenario)
        result = _run(env.env, _base_args(), source_dir=source_checkout)
        assert result.returncode != 0
        assert "hostd" in result.stderr.lower()

    def test_legacy_hostd_port_8741_blocks_acceptance(self, tmp_path, source_checkout):
        scenario = default_scenario()
        scenario["legacy_present"] = {"hostd_port": True}
        env = build_fake_pve_environment(tmp_path, scenario)
        result = _run(env.env, _base_args(), source_dir=source_checkout)
        assert result.returncode != 0
        assert "8741" in result.stderr

    def test_installer_failure_stops_before_config_or_firewall(self, tmp_path, source_checkout):
        scenario = default_scenario()
        scenario["fail"] = ["installer"]
        env = build_fake_pve_environment(tmp_path, scenario)
        result = _run(env.env, _base_args(), source_dir=source_checkout)
        assert result.returncode != 0
        log = env.log_lines()
        assert not any("inventory.yaml" in line for line in log)
        assert not any("nftables" in line for line in log)


# ---------------------------------------------------------------------------
# Security / static checks
# ---------------------------------------------------------------------------


class TestSecurityStatic:
    _ALL_SCRIPTS = [BOOTSTRAP_SCRIPT, *sorted((REPO_ROOT / "deploy" / "lib").glob("*.sh"))]

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

    def test_no_legacy_runtime_surface_guard_remains_green(self):
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "tests/test_no_legacy_runtime_surface.py"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
