from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_bootstrap_keeps_pve_api_privileges_exactly_read_only() -> None:
    identity = _text("deploy/lib/bootstrap-identity.sh")
    assert 'PVE_REQUIRED_PRIVS="Sys.Audit,VM.Audit"' in identity
    assert "VM.Monitor" not in identity
    assert "VM.PowerMgmt" not in identity


def test_forced_command_authorization_is_one_operation_and_restricts_ssh() -> None:
    host_control = _text("deploy/lib/bootstrap-host-control.sh")
    helper = _text("deploy/hubinet-package-scan-helper.py")
    for option in (
        "no-port-forwarding",
        "no-agent-forwarding",
        "no-X11-forwarding",
        "no-pty",
    ):
        assert option in host_control
    assert '"operation"' in helper
    assert '"scan_packages"' in helper
    assert "unknown host-control operation" in helper
    assert "eval(" not in helper
    assert "shell=True" not in helper


def test_bootstrap_pins_host_key_and_uses_restrictive_key_modes() -> None:
    host_control = _text("deploy/lib/bootstrap-host-control.sh")
    assert "/etc/ssh/ssh_host_ed25519_key.pub" in host_control
    assert "known_hosts" in host_control
    assert 'chmod 0600 "${HOST_CONTROL_CT_PRIVATE_KEY}"' in host_control
    assert 'chmod 0600 "${HOST_CONTROL_CT_KNOWN_HOSTS}"' in host_control
    assert "StrictHostKeyChecking=yes" in host_control
    assert "PasswordAuthentication=no" in host_control


def test_rollback_filters_only_unique_hubinet_authorization_and_artifacts() -> None:
    host_control = _text("deploy/lib/bootstrap-host-control.sh")
    assert "index($0, marker) == 0" in host_control
    assert "HOST_CONTROL_AUTH_MARKER" in host_control
    assert 'rm -f "${helper_path}"' in host_control
    # Family 2 correction pass: authorized_keys add/remove now goes through
    # one shared atomic (stage in a temp file, fsync, atomic rename, fsync
    # the containing directory) primitive rather than truncating the live
    # file in place -- the truncate-then-replace pattern this used to pin
    # is exactly the bug that primitive replaces.
    assert "_host_control_authorized_keys_add" in host_control
    assert "_host_control_authorized_keys_remove" in host_control
    assert 'cat "${filtered}" >"${authorized_keys_path}"' not in host_control
    assert 'mv "${filtered}" "${authorized_keys_path}"' not in host_control
    assert '_host_control_secure_root_file "${authorized_keys_path}"' not in host_control
    assert 'chown root:root "${authorized_keys_path}"' not in host_control
    assert 'chmod 0600 "${authorized_keys_path}"' not in host_control
    assert 'rm -rf "${HOST_CONTROL_CT_DIR}"' in host_control
    assert "rm -rf /root/.ssh" not in host_control
    assert "rm -f /root/.ssh/authorized_keys" not in host_control


def test_bootstrap_installs_ssh_client_and_grants_only_pve_ssh_egress() -> None:
    deploy = _text("deploy/lib/bootstrap-deploy.sh")
    firewall = _text("deploy/lib/bootstrap-firewall.sh")
    assert "openssh-client" in deploy
    assert 'ip daddr %s tcp dport 22 accept' in firewall
    assert 'tcp dport 22 accept")' in firewall
    assert "meta skuid" in firewall


def test_generated_config_contains_validated_package_scan_runtime_setting() -> None:
    bootstrap = _text("deploy/bootstrap-proxmox-0.5.sh")
    preflight = _text("deploy/lib/bootstrap-preflight.sh")
    deploy = _text("deploy/lib/bootstrap-deploy.sh")
    assert 'PACKAGE_SCAN_INTERVAL_SECONDS="21600"' in bootstrap
    assert "--package-scan-interval" in bootstrap
    assert "PACKAGE_SCAN_INTERVAL_SECONDS >= 60" in preflight
    assert "PACKAGE_SCAN_INTERVAL_SECONDS <= 604800" in preflight
    assert "package_scan:" in deploy
    assert "interval_seconds: ${PACKAGE_SCAN_INTERVAL_SECONDS}" in deploy


def test_host_helper_is_executable_and_not_a_generic_command_framework() -> None:
    helper = ROOT / "deploy" / "hubinet-package-scan-helper.py"
    assert helper.stat().st_mode & 0o111
    text = helper.read_text(encoding="utf-8")
    assert '"pct", "exec"' in text
    assert '"pvesh", "get", "/cluster/resources"' in text
    assert '"apt-get", "--version"' in text
    assert "apt-get\", \"update\", \"-qq" in text
    assert "apt-get\", \"-s\", \"upgrade" in text
    # Corrective pass, Finding 1: APT's own fail-on-any-error option must be
    # in the fixed metadata-refresh argv so a partial/stale index refresh
    # is a hard failure rather than a silently-accepted stale cache.
    assert "--error-on=any" in text
    assert "request.get(\"command\")" not in text
