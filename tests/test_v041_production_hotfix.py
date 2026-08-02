from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import re
import subprocess
import sys

import pytest
import yaml

from app.contracts import parse_owned_snapshot_name
from app.ha_entities import (
    AGENT_ENTITY_SPECS,
    resource_entity_specs,
)
from app.service import _snapshot_name
from scripts.generate_ha_dashboard import _control_card, _control_conditions
from scripts.migrate_config_0_4_0 import CAPABILITY_KEYS
from scripts.validate_hermetic_shell_boundary import validate_file
from scripts.validate_ha_secrets_0_4_1 import REQUIRED_SECRETS, validate as validate_secrets
from scripts.validate_rollout_state_0_4_1 import validate as validate_rollout


ROOT = Path(__file__).parents[1]
PVE_INSTALLER = ROOT / "deploy" / "upgrade-0.4.1-from-pve.sh"
HA_INSTALLER = ROOT / "deploy" / "install-ha-0.4.1-from-pve.sh"
AGENT_INSTALLER = ROOT / "deploy" / "install-agent.sh"
AGENT_RESTORE = ROOT / "deploy" / "agent" / "restore-0.3.0.sh"
HERMETIC_SHELL_VALIDATOR = ROOT / "scripts" / "validate_hermetic_shell_boundary.py"
SANDBOX_RUNNER = ROOT / "tests" / "shell" / "run_runtime_smoke_sandbox.sh"
SANDBOX_ENTRYPOINT = (
    ROOT / "tests" / "shell" / "runtime_smoke_sandbox_entrypoint.sh"
)
RUNTIME_SMOKE = ROOT / "tests" / "shell" / "runtime_smoke_0_4_1.sh"
HA_RUNTIME_SMOKE = ROOT / "tests" / "shell" / "runtime_smoke_ha_0_4_1.sh"


def _resources() -> dict[int, dict]:
    raw = yaml.safe_load(
        (ROOT / "config" / "config.example.yaml").read_text(encoding="utf-8")
    )
    return {int(vmid): dict(cfg) for vmid, cfg in raw["resources"].items()}


def _fresh_payload() -> dict:
    resources = {}
    for vmid in range(100, 111):
        state = {
            "last_refresh": "2026-07-24T18:30:00+00:00",
            "health_status": "healthy",
            "health_score": 100,
        }
        if vmid == 100:
            state["cpu"] = {"usage_percent": 3.5}
        if 101 <= vmid <= 109:
            state.update(
                executor_compatible=True,
                executor_version="0.4.1",
                executor_protocol_version=1,
            )
        resources[str(vmid)] = state
    return {"version": "0.4.1", "resources": resources}


def test_041_version_and_database_schema_contract() -> None:
    assert 'VERSION = "0.4.3"' in (ROOT / "app" / "mqtt.py").read_text(
        encoding="utf-8"
    )
    assert 'EXECUTOR_VERSION = "0.4.3"' in (
        ROOT / "app" / "contracts.py"
    ).read_text(encoding="utf-8")
    assert 'VERSION = "0.4.3"' in (
        ROOT / "deploy" / "managed" / "hubinet-maint"
    ).read_text(encoding="utf-8")
    assert 'VERSION = "0.4.3"' in (
        ROOT / "deploy" / "pve" / "hubinet_ops_hostd.py"
    ).read_text(encoding="utf-8")
    assert "PRAGMA user_version=400" in (
        ROOT / "app" / "database.py"
    ).read_text(encoding="utf-8")


def test_agent_install_provisions_service_readable_private_ssh_key() -> None:
    text = AGENT_INSTALLER.read_text(encoding="utf-8")
    key = "/etc/hubinet-ops/keys/proxmox_ed25519"

    assert f"if [[ ! -f {key} ]]; then" in text
    assert text.count("ssh-keygen -q -t ed25519") == 1
    assert "normalize_ssh_permissions" in text
    assert "install -d -m 0750 -o root -g hubinetops /etc/hubinet-ops/keys" in text
    assert f"chown hubinetops:hubinetops {key}" in text
    assert f"chmod 0600 {key}" in text
    assert "chown root:hubinetops /etc/hubinet-ops/ssh_known_hosts" in text
    assert "chmod 0640 /etc/hubinet-ops/ssh_known_hosts" in text
    assert not re.search(rf"^chown root:hubinetops {re.escape(key)}$", text, re.MULTILINE)
    assert not re.search(rf"^chmod 0640 {re.escape(key)}$", text, re.MULTILINE)

    private_mode = int("0600", 8)
    assert private_mode & 0o400
    assert private_mode & 0o077 == 0


def test_agent_upgrade_normalizes_existing_ssh_metadata_before_start() -> None:
    text = PVE_INSTALLER.read_text(encoding="utf-8")
    key = "/etc/hubinet-ops/keys/proxmox_ed25519"
    normalize = [
        "install -d -m 0750 -o root -g hubinetops /etc/hubinet-ops/keys",
        f"test -f {key}",
        f"chown hubinetops:hubinetops {key}",
        f"chmod 0600 {key}",
        "test -f /etc/hubinet-ops/ssh_known_hosts",
        "chown root:hubinetops /etc/hubinet-ops/ssh_known_hosts",
        "chmod 0640 /etc/hubinet-ops/ssh_known_hosts",
    ]

    positions = [text.index(line) for line in normalize]
    assert positions == sorted(positions)
    assert positions[-1] < text.index("systemctl start hubinet-ops", positions[-1])
    remote_install = text[text.index("REMOTE_INSTALL_AGENT") :]
    assert "ssh-keygen" not in remote_install
    assert "cat /etc/hubinet-ops/keys/proxmox_ed25519" not in text


def test_agent_rollback_normalizes_ssh_metadata_and_fails_closed() -> None:
    text = AGENT_RESTORE.read_text(encoding="utf-8")

    for fragment in (
        'install -d -m 0750 -o root -g hubinetops "$ssh_key_dir"',
        'chown hubinetops:hubinetops "$ssh_private_key"',
        'chmod 0600 "$ssh_private_key"',
        'chown root:hubinetops "$ssh_known_hosts"',
        'chmod 0640 "$ssh_known_hosts"',
        'echo "Restored agent SSH permissions are incomplete; hubinet-ops remains stopped"',
        "stop_attempted=false",
    ):
        assert fragment in text
    permissions_end = text.index('chmod 0640 "$ssh_known_hosts"')
    assert permissions_end < text.index("service_action start", permissions_end)


def test_hostd_unit_creates_state_and_has_exact_required_pve_write_paths() -> None:
    text = (
        ROOT / "deploy" / "pve" / "hubinet-ops-hostd.service"
    ).read_text(encoding="utf-8")
    assert "ProtectSystem=strict" in text
    assert "StateDirectory=hubinet-ops-hostd" in text
    assert "StateDirectoryMode=0700" in text
    line = next(
        value for value in text.splitlines() if value.startswith("ReadWritePaths=")
    )
    assert set(line.removeprefix("ReadWritePaths=").split()) == {
        "/etc/pve",
        "/var/lib/hubinet-ops-hostd",
        "/run/lock",
        "/var/log/pve/tasks",
        "/run/lxc/lock",
        "/var/lib/lxc",
        "/etc/lvm/archive",
        "/etc/lvm/backup",
    }
    for path in (
        "/var/lib/hubinet-ops-hostd",
        "/var/log/pve/tasks",
        "/run/lxc/lock",
        "/var/lib/lxc",
        "/etc/lvm/archive",
        "/etc/lvm/backup",
    ):
        assert f"pve_path {path}" in PVE_INSTALLER.read_text(
            encoding="utf-8"
        ).replace('"', "")
    assert "rules.seccomp" in text


def test_hostd_lifecycle_has_bounded_seccomp_temp_write_access() -> None:
    unit = (
        ROOT / "deploy" / "pve" / "hubinet-ops-hostd.service"
    ).read_text(encoding="utf-8")
    installer = PVE_INSTALLER.read_text(encoding="utf-8")
    writable = next(
        line for line in unit.splitlines() if line.startswith("ReadWritePaths=")
    ).split()

    assert "ProtectSystem=strict" in unit
    assert "/var/lib/lxc" in writable
    assert "temporary rules.seccomp files created by LXC start/reboot" in unit
    assert 'install -d -o root -g root -m 0755' in installer
    assert '"$(pve_path /var/lib/lxc)"' in installer


def test_pct_retry_is_err_trap_safe_and_deployment_uses_idempotent_install() -> None:
    text = PVE_INSTALLER.read_text(encoding="utf-8")
    helper = text[text.index("pct_retry_129()"):text.index("HOST_DESTINATIONS=")]
    assert "for attempt in 1 2 3" in helper
    assert 'if pct "$@"; then' in helper
    assert '[[ "$rc" -eq 129 ]] || return "$rc"' in helper
    assert "set +e" not in helper
    assert "pct_retry_129 push" in text
    assert "pct_retry_129 exec" in text
    assert "-- install -m 0755" in text
    assert "-- install -m 0644" in text
    assert "mv -f /usr/local/sbin/.hubinet-maint.new" not in text


def test_upgrade_rollback_and_exit_share_secret_stage_cleanup() -> None:
    text = PVE_INSTALLER.read_text(encoding="utf-8")
    helper = text[text.index("cleanup_secret_stages()"):text.index("rollback_all()")]
    rollback = text[text.index("rollback_all()"):text.index("exit_cleanup()")]
    exit_cleanup = text[text.index("exit_cleanup()"):text.index("trap 'rollback_all")]

    assert 'rm -f -- "$TOKEN_STAGE"' in helper
    assert 'rm -f -- "$HOSTD_ENV_STAGE"' in helper
    assert 'TOKEN_STAGE=""' in helper
    assert 'HOSTD_ENV_STAGE=""' in helper
    assert "cleanup_secret_stages || failed=true" in rollback
    assert "if ! cleanup_secret_stages; then" in exit_cleanup


def test_hermetic_deployment_runtime_smoke_test_boundaries() -> None:
    policy = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    smoke = (ROOT / "tests" / "shell" / "runtime_smoke_0_4_1.sh").read_text(
        encoding="utf-8"
    )

    assert (
        "Do not run `apt`, `pct`, `ssh`, Docker, or deployment scripts "
        "as part of repository tests."
    ) not in policy
    for requirement in (
        "system-enforced smoke sandbox",
        "The sandbox is the security boundary",
        "production script must never execute directly on the pytest host",
        "local Linux does not invoke the manager",
        "a marked Linux CI run must fail closed",
        "static shell validator is defense-in-depth",
        "`HUBINET_OPS_TEST_MODE=1`",
        "isolated `PATH` without inheriting the host `PATH`",
        "temporary fake command layer",
        "explicit allowlist of unprivileged local tools",
        "no real network, deployment, container, or hypervisor programs",
        "no production addresses or credentials",
        "real lifecycle or snapshot mutation",
    ):
        assert requirement in policy

    safe_tools_match = re.search(
        r"^SAFE_TOOL_NAMES=\(\s*(.*?)^\)$",
        smoke,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert safe_tools_match is not None
    safe_tools = set(
        re.findall(
            r"^\s*([a-z0-9-]+)\s*$",
            safe_tools_match.group(1),
            re.MULTILINE,
        )
    )
    assert {"bash", "awk", "grep", "gzip", "tar", "sha256sum"} <= safe_tools
    assert not safe_tools & {
        "apt",
        "apt-get",
        "ssh",
        "scp",
        "pct",
        "pvesh",
        "qm",
        "docker",
        "podman",
        "systemctl",
        "curl",
    }

    run_case = smoke[smoke.index("run_case()"):smoke.index("first_line_after()")]
    assert re.search(
        r'isolated_path="\$case_root/bin:\$case_root/safe-bin"',
        run_case,
    )
    assert re.search(r'^\s*PATH="\$isolated_path"\s*\\$', run_case, re.MULTILINE)
    assert ":$PATH" not in run_case
    for marker in (
        "HUBINET_OPS_TEST_MODE=1",
        'HUBINET_OPS_TEST_PVE_ROOT="$test_pve_root"',
        'HUBINET_OPS_BACKUP_ROOT="$test_backup_root"',
        'HOST_PATH_SENTINEL="hubinet-host-path-sentinel"',
        'PATH="$HOST_ONLY_BIN:$ORIGINAL_HOST_PATH" command -v "$HOST_PATH_SENTINEL"',
        'for tool in "$HOST_PATH_SENTINEL" apt apt-get ssh scp pvesh qm docker podman wget',
        'for tool in pct systemctl curl python3 install mktemp mv cp wrapper-smoke',
        'for tool in "${SAFE_TOOL_NAMES[@]}"',
        'cat > "$bin/pct"',
        'cat > "$bin/systemctl"',
        'cat > "$bin/curl"',
        'echo "unsupported fake pct exec: $*" >&2; exit 1',
        'echo "unsupported fake pct: $action" >&2; exit 1',
    ):
        assert marker in smoke

    validator_call = (
        '"$REAL_PYTHON" \\\n'
        '  "$ROOT/scripts/validate_hermetic_shell_boundary.py" \\\n'
        '  "$ROOT/deploy/upgrade-0.4.1-from-pve.sh"'
    )
    assert validator_call in smoke
    assert smoke.index(validator_call) < smoke.index(
        'success_rc="$(run_case success)"'
    )


@pytest.mark.parametrize(
    ("shell_line", "kind", "fragment"),
    (
        (
            "/usr/bin/curl https://example.invalid",
            "absolute executable path",
            "/usr/bin/curl",
        ),
        (
            "if /usr/bin/curl https://example.invalid; then :; fi",
            "absolute executable path",
            "/usr/bin/curl",
        ),
        (
            "elif /usr/bin/curl https://example.invalid; then",
            "absolute executable path",
            "/usr/bin/curl",
        ),
        (
            "while /usr/bin/curl https://example.invalid; do :; done",
            "absolute executable path",
            "/usr/bin/curl",
        ),
        (
            "until /usr/bin/curl https://example.invalid; do :; done",
            "absolute executable path",
            "/usr/bin/curl",
        ),
        (
            "command /usr/bin/ssh example.invalid",
            "absolute executable path",
            "/usr/bin/ssh",
        ),
        (
            "exec /usr/bin/scp file example.invalid:/tmp/",
            "absolute executable path",
            "/usr/bin/scp",
        ),
        (
            "sudo /usr/sbin/pct status 106",
            "absolute executable path",
            "/usr/sbin/pct",
        ),
        (
            "/usr/bin/sudo /usr/sbin/pct status 106",
            "absolute executable path",
            "/usr/bin/sudo",
        ),
        (
            "FOO=bar /bin/systemctl restart example",
            "absolute executable path",
            "/bin/systemctl",
        ),
        (
            "(/usr/bin/wget https://example.invalid)",
            "absolute executable path",
            "/usr/bin/wget",
        ),
        (
            "$(/usr/bin/nc example.invalid 1234)",
            "absolute executable path",
            "/usr/bin/nc",
        ),
        (
            "$EMPTY/usr/bin/curl https://example.invalid",
            "dynamic shell path construction",
            "$EMPTY/usr/bin/curl -> /usr/bin/curl",
        ),
        ("PATH=/usr/bin:/bin curl https://example.invalid", "PATH reference", "PATH="),
        ("PATH+=:/usr/bin", "PATH reference", "PATH+="),
        ("export PATH=/usr/bin:/bin", "PATH reference", "export PATH"),
        ("export   PATH=/usr/bin:/bin", "PATH reference", "export   PATH"),
        ("export\tPATH=/usr/bin:/bin", "PATH reference", "export\tPATH"),
        ("readonly\nPATH=/usr/bin:/bin", "PATH reference", "readonly\nPATH"),
        ("readonly PATH=/usr/bin:/bin", "PATH reference", "readonly PATH"),
        ("declare PATH=/usr/bin:/bin", "PATH reference", "declare PATH"),
        ("typeset PATH=/usr/bin:/bin", "PATH reference", "typeset PATH"),
        ("unset PATH", "PATH reference", "unset PATH"),
        ('echo "$PATH"', "PATH reference", "$PATH"),
        ('echo "${PATH}"', "PATH reference", "${PATH}"),
        (
            "env PATH=/usr/bin:/bin curl https://example.invalid",
            "PATH reference",
            "PATH=",
        ),
        (
            "/usr/bin/env PATH=/usr/bin:/bin curl https://example.invalid",
            "absolute executable path",
            "/usr/bin/env",
        ),
        (
            "/usr/bin/env -i curl https://example.invalid",
            "absolute executable path",
            "/usr/bin/env",
        ),
        (
            "/usr/bin/python3 -c 'print(\"unsafe\")'",
            "absolute executable path",
            "/usr/bin/python3",
        ),
        (
            "/usr/bin/perl -e 'print \"unsafe\"'",
            "absolute executable path",
            "/usr/bin/perl",
        ),
        (
            "/bin/bash -c 'curl https://example.invalid'",
            "absolute executable path",
            "/bin/bash",
        ),
        (
            "/bin/sh -c 'curl https://example.invalid'",
            "absolute executable path",
            "/bin/sh",
        ),
        ("/sbin/reboot", "absolute executable path", "/sbin/reboot"),
        (
            "command -p curl https://example.invalid",
            "command default PATH escape",
            "command -p",
        ),
        (
            "bash -c 'command -p curl https://example.invalid'",
            "command default PATH escape",
            "command -p",
        ),
        (
            "/usr//bin/curl https://example.invalid",
            "absolute executable path",
            "/usr//bin/curl",
        ),
        (
            "/usr/bin/../bin/curl https://example.invalid",
            "absolute executable path",
            "/usr/bin/../bin/curl",
        ),
        (
            "/bin/./sh -c 'curl https://example.invalid'",
            "absolute executable path",
            "/bin/./sh",
        ),
        (
            "exec 3<>/dev/tcp/192.168.4.249/22",
            "Bash network device",
            "/dev/tcp/192.168.4.249/22",
        ),
        (
            "exec 3<>/dev/udp/192.168.4.249/53",
            "Bash network device",
            "/dev/udp/192.168.4.249/53",
        ),
    ),
)
def test_hermetic_shell_boundary_rejects_absolute_commands(
    tmp_path: Path,
    shell_line: str,
    kind: str,
    fragment: str,
) -> None:
    script = tmp_path / "unsafe.sh"
    script.write_text(f"echo safe\n{shell_line}\n", encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(HERMETIC_SHELL_VALIDATOR), str(script)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 1
    assert f"{script}:2: forbidden {kind}: {fragment}" in completed.stderr


@pytest.mark.parametrize(
    ("shell_text", "raw_fragment", "kind", "normalized"),
    (
        (
            "/usr/bi''n/curl https://example.invalid",
            "/usr/bi''n/curl",
            "shell-assembled absolute executable path",
            "/usr/bin/curl",
        ),
        (
            '/usr/bi""n/curl https://example.invalid',
            '/usr/bi""n/curl',
            "shell-assembled absolute executable path",
            "/usr/bin/curl",
        ),
        (
            '/usr/bi"n"/curl https://example.invalid',
            '/usr/bi"n"/curl',
            "shell-assembled absolute executable path",
            "/usr/bin/curl",
        ),
        (
            "/usr/'bin'/curl https://example.invalid",
            "/usr/'bin'/curl",
            "shell-assembled absolute executable path",
            "/usr/bin/curl",
        ),
        (
            "'/usr/bi'n/curl https://example.invalid",
            "'/usr/bi'n/curl",
            "shell-assembled absolute executable path",
            "/usr/bin/curl",
        ),
        (
            '"/usr/bi"n/curl https://example.invalid',
            '"/usr/bi"n/curl',
            "shell-assembled absolute executable path",
            "/usr/bin/curl",
        ),
        (
            "'/usr/bin/curl' https://example.invalid",
            "'/usr/bin/curl'",
            "shell-assembled absolute executable path",
            "/usr/bin/curl",
        ),
        (
            '"/usr/bin/curl" https://example.invalid',
            '"/usr/bin/curl"',
            "shell-assembled absolute executable path",
            "/usr/bin/curl",
        ),
        (
            "/usr/bi\\n/curl https://example.invalid",
            "/usr/bi\\n/curl",
            "shell-assembled absolute executable path",
            "/usr/bin/curl",
        ),
        (
            "/usr/bi\\\nn/curl https://example.invalid",
            "/usr/bi\\\nn/curl",
            "shell-assembled absolute executable path",
            "/usr/bin/curl",
        ),
        (
            "/usr/bi\\\nn/../bin/curl https://example.invalid",
            "/usr/bi\\\nn/../bin/curl",
            "shell-assembled absolute executable path",
            "/usr/bin/curl",
        ),
        (
            "/bin/''bash -c 'curl https://example.invalid'",
            "/bin/''bash",
            "shell-assembled absolute executable path",
            "/bin/bash",
        ),
        (
            "/sbi''n/reboot",
            "/sbi''n/reboot",
            "shell-assembled absolute executable path",
            "/sbin/reboot",
        ),
        (
            '/usr/sbi""n/pct status 106',
            '/usr/sbi""n/pct',
            "shell-assembled absolute executable path",
            "/usr/sbin/pct",
        ),
        (
            "/de''v/tcp/192.168.4.249/22",
            "/de''v/tcp/192.168.4.249/22",
            "shell-assembled Bash network device",
            "/dev/tcp/192.168.4.249/22",
        ),
        (
            "/dev/tc\\\np/192.168.4.249/22",
            "/dev/tc\\\np/192.168.4.249/22",
            "shell-assembled Bash network device",
            "/dev/tcp/192.168.4.249/22",
        ),
        (
            '/de"v"/udp/192.168.4.249/53',
            '/de"v"/udp/192.168.4.249/53',
            "shell-assembled Bash network device",
            "/dev/udp/192.168.4.249/53",
        ),
        (
            "$'/usr/bin/curl' https://example.invalid",
            "$'/usr/bin/curl'",
            "dynamic shell path construction",
            "/usr/bin/curl",
        ),
        (
            "/usr/$'bin'/curl https://example.invalid",
            "/usr/$'bin'/curl",
            "dynamic shell path construction",
            "/usr/bin/curl",
        ),
        (
            '$"/usr/bin/curl" https://example.invalid',
            '$"/usr/bin/curl"',
            "dynamic shell path construction",
            "/usr/bin/curl",
        ),
        (
            "if /usr/bi''n/curl; then :; fi",
            "/usr/bi''n/curl",
            "shell-assembled absolute executable path",
            "/usr/bin/curl",
        ),
        (
            'command /usr/bi""n/ssh host',
            '/usr/bi""n/ssh',
            "shell-assembled absolute executable path",
            "/usr/bin/ssh",
        ),
        (
            "exec /usr/bi\\\nn/scp file host:/tmp/",
            "/usr/bi\\\nn/scp",
            "shell-assembled absolute executable path",
            "/usr/bin/scp",
        ),
        (
            "FOO=bar /bin/''systemctl restart example",
            "/bin/''systemctl",
            "shell-assembled absolute executable path",
            "/bin/systemctl",
        ),
        (
            "$(/usr/bi''n/nc host 1234)",
            "/usr/bi''n/nc",
            "shell-assembled absolute executable path",
            "/usr/bin/nc",
        ),
    ),
)
def test_hermetic_shell_boundary_rejects_shell_assembled_paths(
    tmp_path: Path,
    shell_text: str,
    raw_fragment: str,
    kind: str,
    normalized: str,
) -> None:
    script = tmp_path / "assembled-unsafe.sh"
    script.write_text(f"echo safe\n{shell_text}\n", encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, str(HERMETIC_SHELL_VALIDATOR), str(script)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    displayed = (
        raw_fragment.replace("\r", r"\r")
        .replace("\n", r"\n")
        .replace("\t", r"\t")
    )
    assert completed.returncode == 1
    assert (
        f"{script}:2: forbidden {kind}: {displayed} -> {normalized}"
        in completed.stderr
    )
    assert completed.stderr.count(f"-> {normalized}") == 1


@pytest.mark.parametrize(
    ("shell_text", "raw_fragment"),
    (
        ("'/usr/bin/curl", "'/usr/bin/curl"),
        ("/usr/bin/curl\\", "/usr/bin/curl\\"),
    ),
)
def test_hermetic_shell_boundary_rejects_ambiguous_absolute_words(
    tmp_path: Path,
    shell_text: str,
    raw_fragment: str,
) -> None:
    script = tmp_path / "ambiguous-unsafe.sh"
    script.write_text(f"echo safe\n{shell_text}", encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, str(HERMETIC_SHELL_VALIDATOR), str(script)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 1
    assert (
        f"{script}:2: forbidden ambiguous shell path construction: "
        f"{raw_fragment} -> /usr/bin/curl"
    ) in completed.stderr


@pytest.mark.parametrize(
    ("shell_text", "normalized"),
    (
        (
            r"/usr/bi$'\x6e'/curl https://example.invalid",
            "/usr/bin/curl",
        ),
        (
            r"$'\x2fusr\x2fbin\x2fcurl' https://example.invalid",
            "/usr/bin/curl",
        ),
        (
            r"/usr/b$'\151'n/curl https://example.invalid",
            "/usr/bin/curl",
        ),
        (
            r"/usr/bi$'\u006e'/curl https://example.invalid",
            "/usr/bin/curl",
        ),
        (
            r"/usr/bi$'\U0000006e'/curl https://example.invalid",
            "/usr/bin/curl",
        ),
        (
            r"$'\u2fusr\u2fbin\u2fcurl' https://example.invalid",
            "/usr/bin/curl",
        ),
        (
            r"$'\U2fusr\U2fbin\U2fcurl' https://example.invalid",
            "/usr/bin/curl",
        ),
        (
            r"/usr/bi$'\0'n/curl https://example.invalid",
            "/usr/bin/curl",
        ),
        (
            r"/usr/bi$'\000'n/curl https://example.invalid",
            "/usr/bin/curl",
        ),
        (
            r"/usr/bi$'\x00'n/curl https://example.invalid",
            "/usr/bin/curl",
        ),
        (
            r"/de$'\x76'/tcp/192.168.4.249/22",
            "/dev/tcp/192.168.4.249/22",
        ),
    ),
)
def test_hermetic_shell_boundary_rejects_escaped_dynamic_paths(
    tmp_path: Path,
    shell_text: str,
    normalized: str,
) -> None:
    script = tmp_path / "escaped-dynamic-unsafe.sh"
    script.write_text(f"echo safe\n{shell_text}\n", encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, str(HERMETIC_SHELL_VALIDATOR), str(script)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 1
    assert (
        f"{script}:2: forbidden dynamic shell path construction: "
        in completed.stderr
    )
    assert f"-> {normalized}" in completed.stderr
    assert completed.stderr.count("forbidden dynamic shell path construction") == 1


@pytest.mark.parametrize(
    "shell_text",
    (
        r'/usr/$"bi${part}n"/curl https://example.invalid',
        r'/usr/"bi${part}n"/curl https://example.invalid',
        r"/usr/bi${part}n/curl https://example.invalid",
        r"/usr/bi$1n/curl https://example.invalid",
        r'/usr/"bi$1n"/curl https://example.invalid',
        r"/usr/bi$@n/curl https://example.invalid",
        r'/usr/"bi$*n"/curl https://example.invalid',
        r"/usr/bi$!n/curl https://example.invalid",
        r"/usr/bi${@}n/curl https://example.invalid",
        r'/usr/"bi${1}n"/curl https://example.invalid',
    ),
)
def test_hermetic_shell_boundary_rejects_empty_parameter_path_bypasses(
    tmp_path: Path,
    shell_text: str,
) -> None:
    script = tmp_path / "parameter-unsafe.sh"
    script.write_text(f"echo safe\n{shell_text}\n", encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, str(HERMETIC_SHELL_VALIDATOR), str(script)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 1
    assert (
        f"{script}:2: forbidden dynamic shell path construction: "
        in completed.stderr
    )
    assert "-> /usr/bin/curl" in completed.stderr
    assert completed.stderr.count("forbidden dynamic shell path construction") == 1


@pytest.mark.parametrize(
    "shell_text",
    (
        r"printf '%s' $'line\nbreak'",
        r"target=$'/opt/hubinet-ops/file'",
        r"message=$'use \x2fopt for local data'",
    ),
)
def test_hermetic_shell_boundary_allows_benign_ansi_c_words(
    tmp_path: Path,
    shell_text: str,
) -> None:
    script = tmp_path / "benign-ansi-c.sh"
    script.write_text(f"{shell_text}\n", encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, str(HERMETIC_SHELL_VALIDATOR), str(script)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_hermetic_shell_boundary_reports_multiple_assembled_violations(
    tmp_path: Path,
) -> None:
    script = tmp_path / "multiple-assembled.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "/usr/bi''n/curl https://example.invalid\n"
        "/de\"v\"/tcp/192.0.2.1/443\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [sys.executable, str(HERMETIC_SHELL_VALIDATOR), str(script)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 1
    assert completed.stderr.count("forbidden shell-assembled") == 2
    assert f"{script}:2:" in completed.stderr
    assert f"{script}:3:" in completed.stderr


@pytest.mark.parametrize(
    "shell_line",
    (
        "echo '/usr/bin/curl'",
        "printf '%s\\n' \"/usr/bin/curl\"",
        'message="use /usr/bin/curl only in documentation"',
    ),
)
def test_hermetic_shell_boundary_preserves_fail_closed_quoted_text_policy(
    tmp_path: Path,
    shell_line: str,
) -> None:
    script = tmp_path / "quoted-path.sh"
    script.write_text(f"{shell_line}\n", encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, str(HERMETIC_SHELL_VALIDATOR), str(script)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 1
    assert "/usr/bin/curl" in completed.stderr


@pytest.mark.parametrize(
    "shell_line",
    (
        "curl http://hostd.test/health",
        "pct status 106",
        "systemctl restart hubinet-ops-hostd",
        "/usr/local/sbin/hubinet-ops-host",
        "/root/hubinet-ops-backups",
        "/opt/hubinet-ops/.venv/bin/python",
    ),
)
def test_hermetic_shell_boundary_allows_controlled_references(
    tmp_path: Path,
    shell_line: str,
) -> None:
    script = tmp_path / "safe.sh"
    script.write_text(f"{shell_line}\n", encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(HERMETIC_SHELL_VALIDATOR), str(script)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_current_upgrade_passes_hermetic_shell_boundary() -> None:
    assert validate_file(PVE_INSTALLER) == []
    assert validate_file(HA_INSTALLER) == []


def test_runtime_smoke_uses_system_sandbox_as_the_only_execution_path() -> None:
    pytest_wrapper = (
        ROOT / "tests" / "test_installer_runtime_smoke.py"
    ).read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runner = SANDBOX_RUNNER.read_text(encoding="utf-8")
    entrypoint = SANDBOX_ENTRYPOINT.read_text(encoding="utf-8")
    smoke = RUNTIME_SMOKE.read_text(encoding="utf-8")
    ha_smoke = HA_RUNTIME_SMOKE.read_text(encoding="utf-8")

    assert "SANDBOX_RUNNER" in pytest_wrapper
    assert "runtime_smoke_0_4_1.sh" not in pytest_wrapper
    assert "requires the fail-closed Docker sandbox" in pytest_wrapper
    for marker in (
        "GITHUB_ACTIONS",
        "HUBINET_OPS_EPHEMERAL_CI",
        "RUNNER_ENVIRONMENT",
        "GITHUB_RUN_ID",
    ):
        assert marker in pytest_wrapper
        assert marker in runner
    assert 'HUBINET_OPS_EPHEMERAL_CI: "1"' in workflow
    assert "bash tests/shell/runtime_smoke_0_4_1.sh" not in readme
    assert "tests/shell/run_runtime_smoke_sandbox.sh" in readme
    assert "controlled ephemeral GitHub-hosted runner" in readme
    assert "HUBINET_OPS_SYSTEM_SANDBOX" in smoke
    assert "runtime smoke must execute inside the system sandbox" in smoke
    assert entrypoint.index("sandbox self-test: passed") < entrypoint.index(
        "runtime_smoke_0_4_3.sh"
    )
    assert entrypoint.index("runtime_smoke_0_4_3.sh") < entrypoint.index(
        "runtime_smoke_ha_0_4_3.sh"
    )
    assert "exec /bin/bash" not in entrypoint
    for marker in (
        "HUBINET_OPS_SYSTEM_SANDBOX",
        '[[ "$(id -u)" != 0 ]]',
        'isolated_path="$case_root/fake-bin:$case_root/safe-bin"',
        '"$REAL_ENV" -i',
        'PATH="$isolated_path"',
        'ln -s "$REAL_PYTHON" "$case_root/fake-bin/python3"',
        'cat > "$case_root/fake-bin/ssh"',
        'cat > "$case_root/fake-bin/scp"',
        "unsupported fake ssh invocation",
        "unsupported fake scp invocation",
        "install-failure-no-restart",
        "initial-restart-failure-rollback-restart-success",
        "initial-restart-failure-rollback-restart-failure",
        "success-hostname",
        "success-ipv4",
        "success-ipv6",
        "TEST_EXPECTED_SSH_TARGET",
        "TEST_EXPECTED_SCP_TARGET",
        "REJECTED_SSH",
        '"$ROOT/scripts/validate_hermetic_shell_boundary.py"',
        '"$ROOT/deploy/install-ha-0.4.1-from-pve.sh"',
        "0.4.1 HA installer runtime smoke: passed",
    ):
        assert marker in ha_smoke
    assert ha_smoke.index(
        '"$ROOT/scripts/validate_hermetic_shell_boundary.py"'
    ) < ha_smoke.index("assert_success_target")
    for loose_match in (
        "*test -s /config/secrets.yaml*",
        "*install -d -m 0700 *",
        "*install -m 0644 /config/packages/hubinet_ops.yaml.new*",
        "*ha core restart*",
    ):
        assert loose_match not in ha_smoke
    assert runner.index("docker run --rm") < runner.index(
        "runtime_smoke_sandbox_entrypoint.sh"
    )


def test_ha_installer_is_not_executed_by_host_pytest() -> None:
    source = Path(__file__).read_text(encoding="utf-8")

    for forbidden in (
        "_run_ha_" + "secrets_preflight",
        "_run_ha_" + "restart_rollback_scenario",
        "[bash, str(" + "HA_INSTALLER)",
    ):
        assert forbidden not in source
    assert "runtime_smoke_ha_0_4_1.sh" in source


def test_system_sandbox_has_required_kernel_and_mount_boundaries() -> None:
    runner = SANDBOX_RUNNER.read_text(encoding="utf-8")
    entrypoint = SANDBOX_ENTRYPOINT.read_text(encoding="utf-8")

    for argument in (
        "--network none",
        "--read-only",
        "--cap-drop ALL",
        "--security-opt no-new-privileges=true",
        "--ipc none",
        "--pids-limit 256",
        "--memory 768m",
        "--memory-swap 768m",
        "--cpus 2",
        "--user 65534:65534",
        "dst=/repo,readonly",
        "--tmpfs /tmp:rw,exec,nosuid,nodev,size=768m,mode=1777",
        "--tmpfs /workspace:rw,nosuid,nodev,noexec,size=128m",
    ):
        assert argument in runner
    for forbidden in (
        "--privileged",
        "--network host",
        "--pid host",
        "--pid=host",
        "/var/run/docker.sock:",
        "/run/podman/podman.sock:",
        "src=/,",
        "src=$HOME",
    ):
        assert forbidden not in runner

    for proof in (
        "repository is writable",
        "host filesystem sentinel is visible",
        "host PID namespace is visible",
        "/var/run/docker.sock",
        "/run/podman/podman.sock",
        "/etc/hubinet-ops",
        "/root/.ssh",
        "effective capabilities are not empty",
        "no-new-privileges is not active",
        "/dev/tcp/198.51.100.1/9",
        "/dev/udp/198.51.100.1/53",
        "socket.create_connection",
    ):
        assert proof in entrypoint


def test_runtime_smoke_redirects_every_production_path_to_each_case() -> None:
    smoke = RUNTIME_SMOKE.read_text(encoding="utf-8")
    run_case = smoke[smoke.index("run_case()"):smoke.index("first_line_after()")]

    for variable in (
        "test_pve_root",
        "test_backup_root",
        "test_archive",
        "test_wrapper",
        'TEST_CT_ROOT="$case_root/ct"',
    ):
        assert variable in run_case
    assert '[[ "$test_path" == "$case_root/"* ]]' in run_case


def test_hermetic_shell_boundary_allows_only_exact_first_line_shebang(
    tmp_path: Path,
) -> None:
    script = tmp_path / "safe-shebang.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "curl http://hostd.test/health\n"
        "pct status 106\n"
        "systemctl restart hubinet-ops-hostd\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [sys.executable, str(HERMETIC_SHELL_VALIDATOR), str(script)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    ("text", "line"),
    (
        ("echo before\n#!/usr/bin/env bash\n", 2),
        ("#!/usr/bin/env bash\n/usr/bin/env -i echo unsafe\n", 2),
        ("#!/usr/bin/env bash -e\n", 1),
    ),
)
def test_hermetic_shell_boundary_rejects_nonexact_or_late_shebang(
    tmp_path: Path,
    text: str,
    line: int,
) -> None:
    script = tmp_path / "unsafe-shebang.sh"
    script.write_text(text, encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, str(HERMETIC_SHELL_VALIDATOR), str(script)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 1
    assert (
        f"{script}:{line}: forbidden absolute executable path: /usr/bin/env"
        in completed.stderr
    )


@pytest.mark.parametrize(
    ("text", "raw_fragment"),
    (
        ("#!/usr/bi''n/env bash\n", "#!/usr/bi''n/env"),
        ('#!"/usr/bin/env" bash\n', '#!"/usr/bin/env"'),
        ("#!/usr/bin/env ba''sh\n", None),
    ),
)
def test_hermetic_shell_boundary_does_not_expand_exact_shebang_exception(
    tmp_path: Path,
    text: str,
    raw_fragment: str | None,
) -> None:
    script = tmp_path / "assembled-shebang.sh"
    script.write_text(text, encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, str(HERMETIC_SHELL_VALIDATOR), str(script)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 1
    if raw_fragment is None:
        assert (
            f"{script}:1: forbidden absolute executable path: /usr/bin/env"
            in completed.stderr
        )
    else:
        assert (
            f"{script}:1: forbidden shell-assembled absolute executable path: "
            f"{raw_fragment} -> /usr/bin/env"
            in completed.stderr
        )


def test_hermetic_shell_boundary_reports_multiple_violations(
    tmp_path: Path,
) -> None:
    script = tmp_path / "multiple-unsafe.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "export PATH=/usr/bin:/bin\n"
        "/usr/bin/python3 -c 'print(\"unsafe\")'\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [sys.executable, str(HERMETIC_SHELL_VALIDATOR), str(script)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 1
    assert f"{script}:2: forbidden PATH reference: export PATH" in completed.stderr
    assert (
        f"{script}:3: forbidden absolute executable path: /usr/bin/python3"
        in completed.stderr
    )


@pytest.mark.parametrize("invalid_utf8", (False, True))
def test_hermetic_shell_boundary_reports_unreadable_input_as_exit_2(
    tmp_path: Path,
    invalid_utf8: bool,
) -> None:
    script = tmp_path / "invalid.sh"
    if invalid_utf8:
        script.write_bytes(b"\xff")

    completed = subprocess.run(
        [sys.executable, str(HERMETIC_SHELL_VALIDATOR), str(script)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 2
    assert f"{script}: unable to validate shell boundary:" in completed.stderr


def test_config_migration_is_idempotent_and_rollback_policy_is_consistent(
    tmp_path: Path,
) -> None:
    source = ROOT / "config" / "config.example.yaml"
    first = tmp_path / "first.yaml"
    second = tmp_path / "second.yaml"
    command = [
        sys.executable,
        str(ROOT / "scripts" / "migrate_config_0_4_1.py"),
        str(source),
        str(first),
        "--host-control-url",
        "http://192.0.2.10:8741",
    ]
    subprocess.run(command, check=True, cwd=ROOT)
    command[2:4] = [str(first), str(second)]
    subprocess.run(command, check=True, cwd=ROOT)
    first_data = yaml.safe_load(first.read_text(encoding="utf-8"))
    second_data = yaml.safe_load(second.read_text(encoding="utf-8"))
    assert second_data == first_data
    resources = first_data["resources"]
    assert not any(resources[100]["operator_capabilities"].values())
    for vmid in range(101, 110):
        assert resources[vmid]["operator_capabilities"]["rollback"] is True
        assert resources[vmid]["manual_rollback_allowed"] is True
    assert resources[110]["manual_rollback_allowed"] is False
    assert resources[110]["operator_capabilities"]["self_update"] is True
    assert set(resources[101]["operator_capabilities"]) == set(CAPABILITY_KEYS)


def test_rollout_validation_accepts_payload_over_500kb_without_argv() -> None:
    payload = _fresh_payload()
    payload["diagnostic_padding"] = "x" * 510_000
    serialized = json.dumps(payload)
    command = [
        sys.executable,
        str(ROOT / "scripts" / "validate_rollout_state_0_4_1.py"),
        "2026-07-24T18:00:00+00:00",
    ]
    assert len(" ".join(command)) < 1000
    completed = subprocess.run(
        command,
        input=serialized,
        text=True,
        capture_output=True,
        cwd=ROOT,
        check=False,
    )
    assert len(serialized) >= 500_000
    assert completed.returncode == 0, completed.stderr
    installer = PVE_INSTALLER.read_text(encoding="utf-8")
    assert "printf '%s' \"$states\" |" in installer
    assert 'python3 - "$states"' not in installer


def test_rollout_validation_reports_exact_safe_diagnostics() -> None:
    payload = _fresh_payload()
    payload["version"] = "0.4.0"
    del payload["resources"]["109"]
    payload["resources"]["101"]["last_refresh"] = "2026-07-24T17:00:00+00:00"
    payload["resources"]["102"].update(
        executor_compatible=False,
        executor_version=None,
        executor_protocol_version=None,
    )
    payload["resources"]["100"]["health_status"] = "critical"
    payload["resources"]["106"]["health_status"] = "degraded"
    payload["resources"]["110"]["health_score"] = 50

    errors = validate_rollout(
        payload,
        datetime(2026, 7, 24, 18, 0, tzinfo=UTC),
    )

    joined = "\n".join(errors)
    for marker in (
        "invalid_version",
        "resource_keys=missing:['109']",
        "stale_last_refresh=101(",
        "bad_executors=102(compatible=False,version=None,protocol=None)",
        "bad_vm100",
        "bad_ct106_health",
        "bad_ct110_health",
    ):
        assert marker in joined
    assert "Authorization" not in joined


def test_snapshot_names_fit_pve_limit_and_normalize_old_and_new_aliases() -> None:
    for vmid in (106, 999999):
        name = f"hubinet-ops-{vmid}-pre-20260724T153100Z"
        assert len(name) <= 40
        assert parse_owned_snapshot_name(name, vmid=vmid)["kind"] == "pre-update"
        generated = _snapshot_name(vmid, "pre-update", "20260724T153100Z")
        assert generated == name
        manual = _snapshot_name(vmid, "manual", "20260724T153100Z")
        assert len(manual) <= 40
        assert parse_owned_snapshot_name(manual, vmid=vmid)["kind"] == "manual"
    legacy = "hubinet-ops-106-pre-update-20260724T153100Z"
    assert parse_owned_snapshot_name(legacy, vmid=106)["kind"] == "pre-update"
    assert parse_owned_snapshot_name("foreign-backup", vmid=106) is None


def test_nullable_timestamp_discovery_is_not_a_timestamp_device_class() -> None:
    specs = list(AGENT_ENTITY_SPECS)
    for cfg in _resources().values():
        specs.extend(resource_entity_specs(cfg))
    timestamp_keys = {
        "last_refresh",
        "last_scan",
        "last_update",
        "last_verification",
        "executor_last_checked_at",
        "latest_snapshot_at",
        "lifecycle_started_at",
        "lifecycle_finished_at",
    }
    matched = [spec for spec in specs if spec.key in timestamp_keys]
    assert matched
    for spec in matched:
        assert spec.extra.get("device_class") != "timestamp"
        assert "default('unknown', true)" in spec.value_template


def test_capability_templates_are_defensive_for_missing_objects_and_keys() -> None:
    specs = []
    for cfg in _resources().values():
        specs.extend(resource_entity_specs(cfg))
    capabilities = [spec for spec in specs if spec.key.startswith("capability_")]
    assert capabilities
    for spec in capabilities:
        assert "operator_capabilities | default({})" in spec.value_template
        assert ".get(" in spec.value_template
        assert ", false)" in spec.value_template


def test_dashboard_visibility_uses_plan_status_and_allows_waiting_snapshot() -> None:
    cfg = _resources()[106]
    approve = _control_conditions(106, cfg, "approve")
    approve_states = {
        item["entity"]: item.get("state")
        for item in approve
    }
    assert (
        approve_states["sensor.hubinet_ops_ct106_active_plan_status"]
        == "waiting_approval"
    )
    assert "sensor.hubinet_ops_ct106_operation_status" not in approve_states
    snapshot = _control_conditions(106, cfg, "snapshot_create")
    assert not any(
        item["entity"] == "sensor.hubinet_ops_ct106_operation_status"
        and item.get("state_not") == "waiting_approval"
        for item in snapshot
    )
    assert any(
        item["entity"] == "sensor.hubinet_ops_ct106_active_job_id"
        and item.get("state") == "none"
        for item in snapshot
    )
    assert _control_card(106, "snapshot_create")["tap_action"] == {
        "action": "perform-action",
        "perform_action": "script.hubinet_ops_snapshot_create",
        "data": {"vmid": 106},
        "confirmation": _control_card(106, "snapshot_create")["tap_action"][
            "confirmation"
        ],
    }


def test_ha_secret_contract_reports_all_missing_and_rejects_legacy_urls() -> None:
    example = (
        ROOT / "home-assistant" / "secrets.example.yaml"
    ).read_text(encoding="utf-8")
    assert validate_secrets(example) == []
    errors = validate_secrets(
        "\n".join(
            [
                'hubinet_ops_approve_url: "http://agent/api/v1/plans/approve"',
                'hubinet_ops_reject_url: "http://agent/api/v1/plans/reject"',
            ]
        )
    )
    joined = "\n".join(errors)
    assert "hubinet_ops_force_stop_url" in joined
    assert "hubinet_ops_self_update_plan_url" in joined
    assert "approve-active" in joined
    assert "reject-active" in joined
    assert set(REQUIRED_SECRETS) <= {
        line.split(":", 1)[0]
        for line in example.splitlines()
        if ":" in line and not line.startswith("#")
    }


@pytest.mark.parametrize("source", ["-", "file"])
def test_ha_secret_validator_accepts_stdin_and_file_path(
    tmp_path: Path,
    source: str,
) -> None:
    example = (
        ROOT / "home-assistant" / "secrets.example.yaml"
    ).read_text(encoding="utf-8")
    command = [sys.executable, str(ROOT / "scripts" / "validate_ha_secrets_0_4_1.py")]
    if source == "-":
        command.append("-")
        input_text = example
    else:
        secrets_file = tmp_path / "secrets.yaml"
        secrets_file.write_text(example, encoding="utf-8")
        command.append(str(secrets_file))
        input_text = None

    completed = subprocess.run(
        command,
        cwd=ROOT,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout == ""
    assert completed.stderr == ""


def test_ha_installer_has_safe_optional_core_restart_workflow() -> None:
    text = HA_INSTALLER.read_text(encoding="utf-8")
    assert "[--restart-core]" in text
    assert "ha core check" in text
    assert "if [[ \"$restart_core\" == true ]]" in text
    assert "ha core restart" in text
    assert "ha core info --raw-json" in text
    assert "new scripts are unavailable until Core is restarted" in text
    assert "SUPERVISOR_TOKEN" not in text
    assert """ssh "${SSH_ARGS[@]}" 'cat /config/secrets.yaml' |""" in text
    assert 'validate_ha_secrets_0_4_1.py" -' in text
    assert 'ssh "${SSH_ARGS[@]}" "python3' not in text
    assert not any(
        command in text
        for command in ("pct start 100", "pct stop 100", "qm start 100", "qm stop 100")
    )


def test_ha_installer_normalizes_scp_target_for_ipv6() -> None:
    text = HA_INSTALLER.read_text(encoding="utf-8")

    assert 'SCP_HOST="$HA_HOST"' in text
    assert 'if [[ "$SCP_HOST" == *:* ]]; then' in text
    assert 'SCP_HOST="[$SCP_HOST]"' in text
    assert 'SCP_TARGET="root@$SCP_HOST"' in text
    assert text.count('"${SCP_TARGET}:/config/') == 2
    assert '"root@$HA_HOST:/config/' not in text
