from __future__ import annotations

from datetime import UTC, datetime
import json
import os
from pathlib import Path
import shutil
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
from scripts.validate_ha_secrets_0_4_1 import REQUIRED_SECRETS, validate as validate_secrets
from scripts.validate_rollout_state_0_4_1 import validate as validate_rollout


ROOT = Path(__file__).parents[1]
PVE_INSTALLER = ROOT / "deploy" / "upgrade-0.4.1-from-pve.sh"
HA_INSTALLER = ROOT / "deploy" / "install-ha-0.4.1-from-pve.sh"


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
    assert 'VERSION = "0.4.1"' in (ROOT / "app" / "mqtt.py").read_text(
        encoding="utf-8"
    )
    assert 'EXECUTOR_VERSION = "0.4.1"' in (
        ROOT / "app" / "contracts.py"
    ).read_text(encoding="utf-8")
    assert 'VERSION = "0.4.1"' in (
        ROOT / "deploy" / "managed" / "hubinet-maint"
    ).read_text(encoding="utf-8")
    assert 'VERSION = "0.4.1"' in (
        ROOT / "deploy" / "pve" / "hubinet_ops_hostd.py"
    ).read_text(encoding="utf-8")
    assert "PRAGMA user_version=400" in (
        ROOT / "app" / "database.py"
    ).read_text(encoding="utf-8")


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
        "`HUBINET_OPS_TEST_MODE=1`",
        "temporary fake `PATH`",
        "temporary directories",
        "no real or private-network endpoints",
        "no production addresses or credentials",
        "fails closed on every unsupported command",
        "real lifecycle or snapshot mutation",
    ):
        assert requirement in policy
    for marker in (
        'PATH="$case_root/bin:$PATH"',
        "HUBINET_OPS_TEST_MODE=1",
        'HUBINET_OPS_TEST_PVE_ROOT="$case_root/pve"',
        'HUBINET_OPS_BACKUP_ROOT="$case_root/backups"',
        'echo "unsupported fake pct exec: $*" >&2; exit 1',
        'echo "unsupported fake pct: $action" >&2; exit 1',
    ):
        assert marker in smoke


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


def test_ha_installer_has_safe_optional_core_restart_workflow() -> None:
    text = HA_INSTALLER.read_text(encoding="utf-8")
    assert "[--restart-core]" in text
    assert "ha core check" in text
    assert "if [[ \"$restart_core\" == true ]]" in text
    assert "ha core restart" in text
    assert "ha core info --raw-json" in text
    assert "new scripts are unavailable until Core is restarted" in text
    assert "SUPERVISOR_TOKEN" not in text
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


@pytest.mark.parametrize(
    ("host", "target"),
    [
        ("home-assistant.local", "root@home-assistant.local"),
        ("192.168.4.100", "root@192.168.4.100"),
        ("2001:db8::100", "root@[2001:db8::100]"),
    ],
)
def test_ha_installer_passes_exact_scp_destinations(
    tmp_path: Path,
    host: str,
    target: str,
) -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is unavailable on this platform")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log_path = tmp_path / "scp.args"
    for name, body in {
        "python3": "#!/usr/bin/env bash\nexit 0\n",
        "ssh": "#!/usr/bin/env bash\nexit 0\n",
        "scp": (
            "#!/usr/bin/env bash\n"
            'printf "<%s>" "$@" >> "$HUBINET_OPS_TEST_SCP_LOG"\n'
            'printf "\\n" >> "$HUBINET_OPS_TEST_SCP_LOG"\n'
        ),
    }.items():
        stub = fake_bin / name
        stub.write_text(body, encoding="utf-8", newline="\n")
        stub.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["HUBINET_OPS_TEST_MODE"] = "1"
    env["HUBINET_OPS_TEST_SCP_LOG"] = str(log_path)
    completed = subprocess.run(
        [bash, str(HA_INSTALLER), host, "2222"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    calls = log_path.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 2
    assert calls[0].endswith(
        f"<{target}:/config/packages/hubinet_ops.yaml.new>"
    )
    assert calls[1].endswith(
        f"<{target}:/config/dashboards/hubinet_ops.yaml.new>"
    )
    if ":" in host:
        assert all(f"<root@{host}:/config/" not in call for call in calls)


def _run_ha_restart_rollback_scenario(
    tmp_path: Path,
    *,
    rollback_restart: str,
    restart_requested: bool = True,
) -> subprocess.CompletedProcess[str]:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is unavailable on this platform")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    event_log = tmp_path / "events.log"
    for name, body in {
        "python3": "#!/usr/bin/env bash\nexit 0\n",
        "scp": "#!/usr/bin/env bash\nexit 0\n",
        "ssh": """#!/usr/bin/env bash
set -Eeuo pipefail
remote="${@: -1}"
if [[ "$remote" == *"backup.complete"* ]]; then
  echo "backup complete" >> "$HUBINET_OPS_TEST_EVENT_LOG"
elif [[ "$remote" == *"before-0.4.1/secrets.yaml"* ]]; then
  echo "backup restoration" >> "$HUBINET_OPS_TEST_EVENT_LOG"
  echo "rollback ha core check" >> "$HUBINET_OPS_TEST_EVENT_LOG"
elif [[ "$remote" == *"install -m 0644 /config/packages"* ]]; then
  if [[ "$HUBINET_OPS_TEST_ROLLBACK_RESTART" == "not-requested" ]]; then
    echo "initial install failure" >> "$HUBINET_OPS_TEST_EVENT_LOG"
    exit 1
  fi
elif [[ "$remote" == *"ha core restart"* ]]; then
  count_file="$HUBINET_OPS_TEST_RESTART_COUNT"
  count=$(( $(cat "$count_file" 2>/dev/null || echo 0) + 1 ))
  echo "$count" > "$count_file"
  if [[ "$count" == 1 ]]; then
    echo "initial restart failure" >> "$HUBINET_OPS_TEST_EVENT_LOG"
    exit 1
  fi
  echo "rollback restart" >> "$HUBINET_OPS_TEST_EVENT_LOG"
  if [[ "$HUBINET_OPS_TEST_ROLLBACK_RESTART" == "succeeds" ]]; then
    echo "rollback Core running" >> "$HUBINET_OPS_TEST_EVENT_LOG"
    exit 0
  fi
  echo "rollback restart failure" >> "$HUBINET_OPS_TEST_EVENT_LOG"
  exit 1
fi
exit 0
""",
    }.items():
        stub = fake_bin / name
        stub.write_text(body, encoding="utf-8", newline="\n")
        stub.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["HUBINET_OPS_TEST_MODE"] = "1"
    env["HUBINET_OPS_HA_RESTART_ATTEMPTS"] = "1"
    env["HUBINET_OPS_HA_RESTART_DELAY"] = "0"
    env["HUBINET_OPS_TEST_EVENT_LOG"] = str(event_log)
    env["HUBINET_OPS_TEST_RESTART_COUNT"] = str(tmp_path / "restart.count")
    env["HUBINET_OPS_TEST_ROLLBACK_RESTART"] = rollback_restart
    command = [bash, str(HA_INSTALLER)]
    if restart_requested:
        command.append("--restart-core")
    command.extend(["home-assistant.local", "2222"])
    return subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize("rollback_restart_succeeds", [True, False])
def test_ha_installer_restart_rollback_restores_core_running(
    tmp_path: Path,
    rollback_restart_succeeds: bool,
) -> None:
    completed = _run_ha_restart_rollback_scenario(
        tmp_path,
        rollback_restart="succeeds" if rollback_restart_succeeds else "fails",
    )
    events = (tmp_path / "events.log").read_text(encoding="utf-8").splitlines()

    assert completed.returncode != 0
    assert events.count("backup restoration") == 1
    assert events.count("rollback restart") == 1
    assert events.index("initial restart failure") < events.index("backup restoration")
    assert events.index("backup restoration") < events.index("rollback ha core check")
    assert events.index("rollback ha core check") < events.index("rollback restart")
    if rollback_restart_succeeds:
        assert events.index("rollback restart") < events.index("rollback Core running")
        assert "ROLLBACK INCOMPLETE" not in completed.stderr
    else:
        assert "rollback Core running" not in events
        assert (
            "ROLLBACK INCOMPLETE: HA files were restored but Core did not return to running"
            in completed.stderr
        )


def test_ha_installer_restart_rollback_is_not_forced_without_flag(
    tmp_path: Path,
) -> None:
    completed = _run_ha_restart_rollback_scenario(
        tmp_path,
        rollback_restart="not-requested",
        restart_requested=False,
    )
    events = (tmp_path / "events.log").read_text(encoding="utf-8").splitlines()

    assert completed.returncode != 0
    assert events.count("backup restoration") == 1
    assert "rollback restart" not in events
    assert "rollback Core running" not in events
    assert "ROLLBACK INCOMPLETE" not in completed.stderr
