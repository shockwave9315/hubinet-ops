"""Hermetic fixture builder for testing deploy/update-proxmox-0.5.sh.

Seeds a synthetic "already bootstrapped" Hubinet installation directly into
the same hermetic fake-command layer tests/_bootstrap_fake_pve.py provides
for the bootstrap smoke suite, without actually running the real bootstrap
script (which would be slow and is already covered by its own smoke
suite). The seeded state reproduces exactly the ownership chain
deploy/lib/update-ownership.sh verifies: a CT host-control public-key
comment, a PVE-host authorized_keys forced-command line, and PVE user/
token comments, all carrying the same run-id marker; plus the installed
app/venv/unit/config/authority-database files an update needs to classify
and, on approval, replace.

Per AGENTS.md's test-boundary rules, this module never contacts real PVE,
LXC, or any network endpoint -- everything here is plain local file
writes into the same tmp_path-rooted fake CT filesystem
tests/_bootstrap_fake_pve.py's own dispatcher reads and writes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from _bootstrap_fake_pve import (
    FakePveEnvironment,
    build_fake_pve_environment,
    default_scenario,
)

FAKE_VMID = "110"
FAKE_RUN_ID = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
FAKE_BACKEND_INSTANCE_ID = "00000000-0000-4000-8000-000000000001"
FAKE_SCHEMA_MARKER = "hubinet_ops_0_5_authority"
FAKE_HELPER_HOST_PATH_TEMPLATE = "/usr/local/libexec/hubinet-package-scan-helper-{run_id}"
# P2-C: the default authority schema-object set both sides of the fixture
# agree on -- seed_installed_environment writes this into the fake
# authority.db's own "schema_objects" fact (what the live DB actually
# has), and build_update_target_checkout embeds the SAME names into the
# fake target's store.py _REQUIRED_TABLES (what update-plan.sh's static
# extraction expects) -- so the default "preserve" scenario across every
# existing test stays internally coherent unless a test deliberately
# passes a different set on one side to exercise a structural mismatch.
FAKE_REQUIRED_SCHEMA_OBJECTS = ("authority_schema", "backend_instance", "one_active_endpoint_per_source")


def seed_installed_environment(
    tmp_path: Path,
    *,
    vmid: str = FAKE_VMID,
    run_id: str = FAKE_RUN_ID,
    schema_version: int = 9,
    backend_instance_id: str = FAKE_BACKEND_INSTANCE_ID,
    scenario_overrides: dict[str, Any] | None = None,
    installed_source_sha: str | None = None,
    installed_requirements: str = "fastapi==0.116.1\n",
    installed_unit_text: str | None = None,
    installed_helper_text: str | None = None,
    corrupt_authority_db: bool = False,
    missing_authority_db: bool = False,
    schema_objects: list[str] | None = None,
) -> FakePveEnvironment:
    scenario = default_scenario()
    scenario["update_probe_backend_instance_id"] = backend_instance_id
    scenario["discovery_backend_instance_id"] = backend_instance_id
    # A successful update is expected to complete at least one genuine
    # post-restart discovery cycle -- the pre-update probe reports
    # sequence 1, post-update acceptance reports a later sequence, so the
    # default scenario represents a CORRECTLY functioning installation
    # rather than accidentally exercising the "no genuine new cycle
    # happened" fail-closed path on every test.
    scenario["update_probe_sequence"] = 1
    scenario["discovery_committed_sequence"] = 2
    if scenario_overrides:
        scenario.update(scenario_overrides)

    env = build_fake_pve_environment(tmp_path, scenario)
    helper_path = FAKE_HELPER_HOST_PATH_TEMPLATE.format(run_id=run_id)
    marker = f"hubinet-ops-package-scan-vmid-{vmid}-{run_id}"

    # -- PVE identity state (state.json) -----------------------------------
    state = json.loads(env.state_path.read_text(encoding="utf-8"))
    state["vmids"][vmid] = {
        "started": True,
        "onboot": "1",
        "features": "nesting=1",
        "template": "debian-13-standard_13.6-1_amd64.tar.zst",
        "arch": "amd64",
        "service": "active",
        "service_enabled": True,
        # A freshly bootstrapped installation's enablement is already
        # durable (the real bootstrap installer's own systemd activation
        # long predates any in-place update run) -- see correction pass
        # 10's durable_service_enabled/simulate_pve_ct_reboot model in
        # tests/_bootstrap_fake_pve.py.
        "durable_service_enabled": True,
        "nftables_active": True,
    }
    state["pve_users"]["hubinetops@pve"] = {
        "comment": f"Hubinet Ops 0.5 R0 read-only discovery (created by bootstrap-proxmox-0.5.sh; run={run_id})"
    }
    state["pve_roles"]["HubinetOpsR0Auditor"] = ["Sys.Audit", "VM.Audit"]
    state["pve_tokens"]["hubinetops@pve!r0-readonly"] = {
        "comment": f"R0 read-only discovery token (created by bootstrap-proxmox-0.5.sh; run={run_id})"
    }
    state["acl_grants"] = [
        {"target": "user:hubinetops@pve", "role": "HubinetOpsR0Auditor"},
        {"target": "token:hubinetops@pve!r0-readonly", "role": "HubinetOpsR0Auditor"},
    ]
    env.state_path.write_text(json.dumps(state), encoding="utf-8")

    # -- Host-side files (authorized_keys, PVE host helper) ----------------
    host_root = Path(env.env["HUBINET_OPS_TEST_HOST_ROOT"])
    ssh_dir = host_root / "root" / ".ssh"
    ssh_dir.mkdir(parents=True, exist_ok=True)
    authorized_keys = ssh_dir / "authorized_keys"
    authorized_keys.write_text(
        f'command="{helper_path}",no-port-forwarding,no-agent-forwarding,'
        f'no-X11-forwarding,no-pty ssh-ed25519 QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE= {marker}\n',
        encoding="utf-8",
    )
    helper_host_path = host_root / helper_path.lstrip("/")
    helper_host_path.parent.mkdir(parents=True, exist_ok=True)
    helper_text = installed_helper_text
    if helper_text is None:
        helper_text = (Path(__file__).resolve().parents[1] / "deploy" / "hubinet-package-scan-helper.py").read_text(
            encoding="utf-8"
        )
    helper_host_path.write_text(helper_text, encoding="utf-8")
    helper_host_path.chmod(0o755)

    # -- CT-side files (app/venv/unit/config/authority db) -----------------
    def ct(path: str) -> Path:
        return env.ct_file(vmid, path)

    hc_dir = ct("/etc/hubinet-ops/host-control")
    hc_dir.mkdir(parents=True, exist_ok=True)
    (hc_dir / "id_ed25519").write_text("FAKE PRIVATE KEY FOR TEST ONLY\n", encoding="utf-8")
    (hc_dir / "id_ed25519.pub").write_text(
        f"ssh-ed25519 QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE= {marker}\n", encoding="utf-8"
    )
    (hc_dir / "known_hosts").write_text(
        "192.0.2.10 ssh-ed25519 QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE=\n", encoding="utf-8"
    )

    inventory_yaml = ct("/etc/hubinet-ops/inventory.yaml")
    inventory_yaml.parent.mkdir(parents=True, exist_ok=True)
    inventory_yaml.write_text(
        'source:\n  display_name: "Home Proxmox"\n  provider_kind: proxmox_ve\n', encoding="utf-8"
    )
    (ct("/etc/hubinet-ops/agent.env")).write_text(
        "HUBINET_OPS_R0_CONFIG=/etc/hubinet-ops/inventory.yaml\n"
        "HUBINET_OPS_R0_PVE_TOKEN=hubinetops@pve!r0-readonly=00000000-0000-0000-0000-000000000000\n"
        "HUBINET_OPS_R0_API_TOKEN=" + "f" * 64 + "\n",
        encoding="utf-8",
    )

    app_dir = ct("/opt/hubinet-ops/app")
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / "__init__.py").write_text("", encoding="utf-8")
    (ct("/opt/hubinet-ops/requirements.txt")).write_text(installed_requirements, encoding="utf-8")
    venv_bin = ct("/opt/hubinet-ops/.venv/bin")
    venv_bin.mkdir(parents=True, exist_ok=True)
    (venv_bin / "pip").write_text("#!/bin/sh\n", encoding="utf-8")
    (venv_bin / "python3").write_text("#!/bin/sh\n", encoding="utf-8")

    unit_dir = ct("/etc/systemd/system")
    unit_dir.mkdir(parents=True, exist_ok=True)
    if installed_unit_text is None:
        installed_unit_text = (
            Path(__file__).resolve().parents[1] / "deploy" / "hubinet-ops-0.5.service"
        ).read_text(encoding="utf-8")
    (unit_dir / "hubinet-ops.service").write_text(installed_unit_text, encoding="utf-8")

    var_lib = ct("/var/lib/hubinet-ops")
    var_lib.mkdir(parents=True, exist_ok=True)
    if missing_authority_db:
        pass
    elif corrupt_authority_db:
        (var_lib / "authority.db").write_text("not-json-and-not-sqlite", encoding="utf-8")
    else:
        (var_lib / "authority.db").write_text(
            json.dumps({
                "marker": FAKE_SCHEMA_MARKER,
                "schema_version": schema_version,
                "backend_instance_id": backend_instance_id,
                "schema_objects": sorted(
                    schema_objects if schema_objects is not None else FAKE_REQUIRED_SCHEMA_OBJECTS
                ),
            }),
            encoding="utf-8",
        )

    # A structurally complete ruleset -- both the loopback-ingress and the
    # established/related-reply-egress invariants the fake's own
    # _firewall_permits_local_and_reply_traffic requires (mirroring real
    # deploy/lib/bootstrap-firewall.sh's generated shape) must be present,
    # or the fake's simulated GET /r0/v1/health and discovery-acceptance
    # calls would appear to hang exactly as they would against a real,
    # genuinely misconfigured firewall.
    (ct("/etc/nftables.conf")).write_text(
        "table inet hubinet_ops_r0 {\n"
        "  chain input {\n"
        "    type filter hook input priority 0; policy accept;\n"
        '    iifname "lo" accept\n'
        "    ip saddr 192.0.2.50 tcp dport 8787 accept\n"
        "    tcp dport 8787 drop\n"
        "  }\n"
        "  chain output {\n"
        "    type filter hook output priority 0; policy accept;\n"
        "    ct state established,related accept\n"
        '    meta skuid "hubinetops" ip daddr 192.0.2.10 tcp dport 8006 accept\n'
        '    meta skuid "hubinetops" ip daddr 192.0.2.10 tcp dport 22 accept\n'
        '    meta skuid "hubinetops" drop\n'
        "  }\n"
        "}\n",
        encoding="utf-8",
    )

    if installed_source_sha is not None:
        (ct("/opt/hubinet-ops/.hubinet-source-commit")).write_text(installed_source_sha + "\n", encoding="utf-8")

    return env


def build_update_target_checkout(
    tmp_path: Path,
    repo_root: Path,
    *,
    schema_version: int = 9,
    requirements_text: str = "fastapi==0.116.1\n",
    unit_text: str | None = None,
    helper_text: str | None = None,
    required_schema_objects: list[str] | None = None,
) -> Path:
    """A tiny, fast, git-initialized target checkout for --source-dir.

    Includes exactly the files deploy/update-proxmox-0.5.sh and its lib
    modules actually read from a target commit: requirements.txt,
    deploy/hubinet-ops-0.5.service, deploy/hubinet-package-scan-helper.py,
    and a minimal app/inventory/store.py carrying the two authority-schema
    constants the updater statically reads. `git` is the real system
    binary (never faked) -- see _bootstrap_fake_pve.py's own docstring for
    why source-provenance tests need real git behavior.
    """

    src = tmp_path / "update-target-checkout"
    (src / "app" / "inventory").mkdir(parents=True)
    (src / "deploy").mkdir(parents=True)
    (src / "app" / "__init__.py").write_text("", encoding="utf-8")
    (src / "app" / "inventory" / "__init__.py").write_text("", encoding="utf-8")
    objects = sorted(
        required_schema_objects if required_schema_objects is not None else FAKE_REQUIRED_SCHEMA_OBJECTS
    )
    objects_literal = "".join(f'        "{name}",\n' for name in objects)
    (src / "app" / "inventory" / "store.py").write_text(
        '"""Fake target store.py for update-proxmox-0.5.sh tests."""\n\n'
        'AUTHORITY_SCHEMA_MARKER = "hubinet_ops_0_5_authority"\n'
        f'AUTHORITY_SCHEMA_VERSION = {schema_version}\n\n'
        # Deliberately mirrors the real app/inventory/store.py shape
        # (_REQUIRED_TABLES unioned into _REQUIRED_SCHEMA_OBJECTS, followed
        # by _LEGACY_TABLES) -- deploy/lib/update-plan.sh's
        # _update_target_authority_schema statically scans exactly this
        # text shape (see AGENTS.md P2-C) to preflight-validate a
        # would-be schema-preserving update; it never imports this file.
        "_REQUIRED_TABLES = frozenset(\n"
        "    {\n"
        f"{objects_literal}"
        "    }\n"
        ")\n"
        "_REQUIRED_SCHEMA_OBJECTS = _REQUIRED_TABLES\n"
        '_LEGACY_TABLES = frozenset({"plans", "jobs"})\n',
        encoding="utf-8",
    )
    (src / "requirements.txt").write_text(requirements_text, encoding="utf-8")
    if unit_text is None:
        unit_text = (repo_root / "deploy" / "hubinet-ops-0.5.service").read_text(encoding="utf-8")
    (src / "deploy" / "hubinet-ops-0.5.service").write_text(unit_text, encoding="utf-8")
    if helper_text is None:
        helper_text = (repo_root / "deploy" / "hubinet-package-scan-helper.py").read_text(encoding="utf-8")
    (src / "deploy" / "hubinet-package-scan-helper.py").write_text(helper_text, encoding="utf-8")
    (src / "deploy" / "install-0.5.0-fresh.sh").write_text(
        (repo_root / "deploy" / "install-0.5.0-fresh.sh").read_text(encoding="utf-8"), encoding="utf-8"
    )

    import subprocess as _subprocess

    for cmd in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "test@example.invalid"],
        ["git", "config", "user.name", "test"],
        ["git", "add", "-A"],
        ["git", "commit", "-q", "-m", "target"],
    ):
        _subprocess.run(cmd, cwd=str(src), check=True, capture_output=True)

    return src
