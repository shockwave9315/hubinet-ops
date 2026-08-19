"""Hermetic fake PVE command layer for testing deploy/bootstrap-proxmox-0.5.sh.

Not a test file itself (no test_ functions) -- a fixture/helper module used
by tests/test_bootstrap_proxmox_0_5.py.

Per AGENTS.md's test-boundary rules, tests must never invoke real `pct`,
`pveum`, `pveam`, `pvesh`, `pvesm`, `nft`, or contact any real network/PVE
endpoint. This module builds a temporary PATH containing fake replacements
for exactly those command names (nothing else on PATH is touched -- real
`bash`, `awk`, `grep`, `sed`, `git`, `tar`, `python3`, etc. remain the real
system binaries, none of which contact PVE or a private network on their
own). The bootstrap script under test is executed as a real subprocess
against this PATH; nothing about the *bootstrap script's own logic* is
mocked -- only the privileged/external commands it shells out to.

The fake layer is a single Python dispatcher (`_dispatcher.py`) driven by a
JSON scenario file; each fake command is a 3-line shell shim that execs the
dispatcher with its own argv[0] basename so the dispatcher knows which
command it is emulating. A simulated per-VMID container filesystem lives
under a scratch directory so `pct push`/`pct exec ... cat ...` round-trip
realistically (needed because later phases read back files earlier phases
wrote, e.g. agent.env).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import stat
import sys
import textwrap
from typing import Any

FAKE_PVE_TOKEN_SECRET = "00000000-0000-0000-0000-000000000000"
FAKE_HA_SOURCE_CIDR = "192.0.2.50/32"
FAKE_PVE_ENDPOINT_HOST = "192.0.2.10"
FAKE_PVE_ENDPOINT = f"https://{FAKE_PVE_ENDPOINT_HOST}:8006"
FAKE_CT_IP = "192.0.2.200"
FAKE_BRIDGE = "vmbr0"
FAKE_STORAGE = "local-lxc"
FAKE_TEMPLATE_STORAGE = "local"
FAKE_TEMPLATE_FILENAME = "debian-13-standard_13.6-1_amd64.tar.zst"
FAKE_TEMPLATE_VOLID = f"{FAKE_TEMPLATE_STORAGE}:vztmpl/{FAKE_TEMPLATE_FILENAME}"

_DISPATCHER_SOURCE = r'''
import json
import os
import shutil
import sys
from pathlib import Path

FAKE_STORAGE = "local-lxc"
FAKE_TEMPLATE_STORAGE = "local"
FAKE_TEMPLATE_FILENAME = "debian-13-standard_13.6-1_amd64.tar.zst"
FAKE_TEMPLATE_VOLID = f"{FAKE_TEMPLATE_STORAGE}:vztmpl/{FAKE_TEMPLATE_FILENAME}"
FAKE_BRIDGE = "vmbr0"

LOG = Path(os.environ["HUBINET_FAKE_LOG"])
SCENARIO = json.loads(Path(os.environ["HUBINET_FAKE_SCENARIO"]).read_text())
CT_ROOT = Path(os.environ["HUBINET_FAKE_CT_ROOT"])
STATE_PATH = Path(os.environ["HUBINET_FAKE_STATE"])


def _load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"vmids": {}, "pve_users": [], "pve_roles": [], "pve_tokens": []}


def _save_state(state):
    STATE_PATH.write_text(json.dumps(state))


def _log(*parts):
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(" ".join(str(p) for p in parts) + "\n")


def _fail(kind):
    return kind in SCENARIO.get("fail", [])


_CT_PATH_ANCHORS = (
    "etc/hubinet-ops",
    "var/lib/hubinet-ops",
    "tmp/hubinet-ops-src",
    "tmp/nftables.conf",
    "tmp/hubinet-ops-src.tar.gz",
)


def _normalize_ct_arg(raw):
    # Windows Git-Bash only: a POSIX-looking absolute-path argument like
    # "/etc/hubinet-ops/inventory.yaml" can be auto-mangled by MSYS into a
    # real (and here, unwriteable) Windows path such as
    # "C:\Program Files\Git\etc\hubinet-ops\..." before this dispatcher
    # (a native, non-MSYS python.exe) ever sees it. These CT-side path
    # arguments are opaque identifiers into our own simulated per-VMID
    # filesystem, never real host paths -- so instead of relying on
    # exactly which conversion (if any) happened, find the last known
    # anchor substring and treat everything from there onward as the
    # canonical relative path, restoring the leading "/" so both the
    # returned string and any comparison against a literal "/etc/..."
    # constant behave identically to the unmangled-on-Linux case. This is
    # a no-op on Linux, where the argument always arrives unmangled.
    normalized = raw.replace("\\", "/")
    for anchor in _CT_PATH_ANCHORS:
        idx = normalized.rfind(anchor)
        if idx != -1:
            return "/" + normalized[idx:]
    if normalized.startswith("/"):
        return normalized
    parts = normalized.split("/")
    for i, part in enumerate(parts):
        if part in ("etc", "var", "tmp"):
            return "/" + "/".join(parts[i:])
    return raw


def _ct_path(vmid, ct_path):
    rel = _normalize_ct_arg(ct_path).lstrip("/")
    return CT_ROOT / str(vmid) / rel


def _resolve_host_path(path):
    # Real host file the fake dispatcher needs to actually read (e.g.
    # `pct push`'s source argument). Windows Git-Bash only: if MSYS
    # converted or failed to convert this to something python.exe can't
    # open, fall back to asking the real `cygpath` binary (shipped with
    # Git for Windows) to resolve it. No-op / never triggered on Linux,
    # where the path is already correct.
    p = Path(path)
    if p.exists():
        return p
    try:
        import subprocess

        converted = subprocess.run(
            ["cygpath", "-w", path], capture_output=True, text=True, timeout=5
        )
        candidate = Path(converted.stdout.strip())
        if candidate.exists():
            return candidate
    except Exception:
        pass
    return p


def cmd_pct(args):
    if not args:
        sys.exit(2)
    sub = args[0]
    state = _load_state()
    # Normalize CT-path-shaped arguments for the log (see _normalize_ct_arg)
    # so assertions against the log are stable regardless of whether this
    # particular arg happened to be MSYS-mangled on a Windows dev machine.
    # A no-op for every non-path-shaped argument.
    _log("pct", *(_normalize_ct_arg(a) for a in args))

    if sub == "status":
        vmid = args[1]
        if vmid in state["vmids"]:
            print(f"status: {'running' if state['vmids'][vmid].get('started') else 'stopped'}")
            sys.exit(0)
        sys.exit(1)

    if sub == "create":
        vmid = args[1]
        if _fail("pct_create"):
            sys.exit(1)
        state["vmids"][vmid] = {"started": False, "onboot": "0", "features": ""}
        _save_state(state)
        sys.exit(0)

    if sub == "set":
        vmid = args[1]
        rest = args[2:]
        entry = state["vmids"].setdefault(vmid, {"started": False, "onboot": "0", "features": ""})
        if "--onboot" in rest:
            entry["onboot"] = rest[rest.index("--onboot") + 1]
        if "--features" in rest:
            entry["features"] = rest[rest.index("--features") + 1]
        _save_state(state)
        sys.exit(0)

    if sub == "start":
        vmid = args[1]
        if _fail("pct_start"):
            sys.exit(1)
        state["vmids"].setdefault(vmid, {"started": False, "onboot": "0", "features": ""})["started"] = True
        _save_state(state)
        sys.exit(0)

    if sub == "stop":
        vmid = args[1]
        state["vmids"].get(vmid, {})["started"] = False
        _save_state(state)
        sys.exit(0)

    if sub == "destroy":
        vmid = args[1]
        state["vmids"].pop(vmid, None)
        _save_state(state)
        shutil.rmtree(CT_ROOT / vmid, ignore_errors=True)
        sys.exit(0)

    if sub == "push":
        vmid, host_path, ct_path = args[1], args[2], args[3]
        if _fail("pct_push"):
            sys.exit(1)
        dest = _ct_path(vmid, ct_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_resolve_host_path(host_path), dest)
        sys.exit(0)

    if sub == "exec":
        vmid = args[1]
        assert args[2] == "--", args
        inner = args[3:]
        sys.exit(_exec_inner(vmid, inner, state))

    _log("pct", "UNHANDLED", *args)
    sys.exit(2)


def _exec_inner(vmid, inner, state):
    joined = " ".join(inner)
    ct = str(vmid)

    if inner[:2] == ["mkdir", "-p"]:
        _ct_path(vmid, inner[2]).mkdir(parents=True, exist_ok=True)
        return 0

    if inner[0] == "tar":
        # Extraction is not content-inspected by any test assertion; a
        # no-op is sufficient and keeps the fake hermetic.
        return 0

    if inner[0] == "rm":
        return 0

    if inner[:2] == ["chown", "root:hubinetops"] or inner[0] == "chown":
        return 0

    if inner[0] == "chmod":
        return 0

    if inner[0] == "cat":
        path = _ct_path(vmid, inner[1])
        if not path.exists():
            return 1
        sys.stdout.write(path.read_text())
        return 0

    if inner[0] == "test" and inner[1] == "-e":
        path = _ct_path(vmid, inner[2])
        marker = SCENARIO.get("legacy_present", {})
        # Normalize before comparing, not just when resolving the path on
        # disk (see _normalize_ct_arg) -- an exact-literal comparison
        # against "/var/lib/hubinet-ops/ops.db" would silently never match
        # if this argument happened to arrive MSYS-mangled.
        if _normalize_ct_arg(inner[2]) == "/var/lib/hubinet-ops/ops.db" and marker.get("ops_db"):
            return 0
        return 0 if path.exists() else 1

    if inner[0] == "bash" and "install-0.5.0-fresh.sh" in joined:
        if _fail("installer"):
            sys.stderr.write("fake installer failure\n")
            return 1
        agent_env = _ct_path(vmid, "/etc/hubinet-ops/agent.env")
        agent_env.parent.mkdir(parents=True, exist_ok=True)
        agent_env.write_text(
            "HUBINET_OPS_R0_CONFIG=/etc/hubinet-ops/inventory.yaml\n"
            "HUBINET_OPS_R0_PVE_TOKEN=\n"
            f"HUBINET_OPS_R0_API_TOKEN={SCENARIO.get('r0_api_token', 'f' * 64)}\n"
        )
        return 0

    if inner[0] == "systemctl":
        return _exec_systemctl(vmid, inner[1:], state)

    if inner[0] == "ss":
        return _exec_ss(vmid, inner[1:], state)

    if inner[0] == "nft":
        return _exec_nft(vmid, inner[1:])

    if inner[0] == "curl":
        return _exec_curl(vmid, inner[1:])

    if inner[0] == "hostname" and inner[1:] == ["-I"]:
        ip = SCENARIO.get("ct_ip")
        if ip:
            sys.stdout.write(ip + "\n")
            return 0
        return 1

    _log("pct", "exec", "UNHANDLED", *inner)
    return 2


def _exec_systemctl(vmid, args, state):
    entry = state["vmids"].setdefault(vmid, {})
    if args[:1] == ["enable"] and "--now" in args and "hubinet-ops" in args:
        if _fail("service_enable"):
            return 1
        entry["service"] = "active"
        entry["service_enabled"] = True
        _save_state(state)
        return 0
    if args[:1] == ["disable"] and "--now" in args and "hubinet-ops" in args:
        entry["service"] = "inactive"
        entry["service_enabled"] = False
        _save_state(state)
        return 0
    if args == ["is-active", "hubinet-ops"]:
        state_val = SCENARIO.get("service_active_override", entry.get("service", "inactive"))
        sys.stdout.write(state_val + "\n")
        return 0 if state_val == "active" else 3
    if args == ["is-enabled", "hubinet-ops"]:
        enabled = entry.get("service_enabled", False)
        sys.stdout.write(("enabled" if enabled else "disabled") + "\n")
        return 0 if enabled else 1
    if args == ["is-system-running"]:
        sys.stdout.write(SCENARIO.get("systemd_status", "running") + "\n")
        return 0
    if args[:1] == ["--failed"]:
        for unit in SCENARIO.get("failed_units", []):
            sys.stdout.write(unit + "\n")
        return 0
    if args == ["status", "hubinet-ops-hostd"]:
        return 0 if SCENARIO.get("legacy_present", {}).get("hostd") else 1
    if args[:1] == ["enable"] and "nftables" in args:
        return 0
    if args[:1] == ["restart"] and "nftables" in args:
        if _fail("nft_activate"):
            return 1
        return 0
    _log("systemctl", "UNHANDLED", *args)
    return 2


def _exec_ss(vmid, args, state):
    entry = state["vmids"].get(vmid, {})
    lines = []
    if entry.get("service") == "active":
        lines.append("LISTEN 0 128 0.0.0.0:8787 0.0.0.0:*")
    if SCENARIO.get("legacy_present", {}).get("hostd_port"):
        lines.append("LISTEN 0 128 0.0.0.0:8741 0.0.0.0:*")
    sys.stdout.write("\n".join(lines) + ("\n" if lines else ""))
    return 0


def _exec_nft(vmid, args):
    if args[:2] == ["-c", "-f"]:
        if _fail("nft_syntax"):
            return 1
        return 0
    if args == ["list", "ruleset"]:
        path = _ct_path(vmid, "/etc/nftables.conf")
        if path.exists():
            sys.stdout.write(path.read_text())
        return 0
    return 2


def _exec_curl(vmid, args):
    url = args[-1]
    if "/r0/v1/health" in url:
        if _fail("backend_health"):
            return 7
        sys.stdout.write(SCENARIO.get("health_body", '{"status": "ok"}'))
        return 0
    if "/r0/v1/snapshot" in url:
        if _fail("backend_snapshot"):
            return 7
        sys.stdout.write(SCENARIO.get(
            "snapshot_body",
            '{"sources": [{"display_name": "Home Proxmox"}], "resources": []}',
        ))
        return 0
    return 7


def cmd_pveum(args):
    _log("pveum", *args)
    state = _load_state()

    if args[:2] == ["user", "list"]:
        print(json.dumps([{"userid": u} for u in state["pve_users"]]))
        return 0
    if args[:2] == ["role", "list"]:
        print(json.dumps([{"roleid": r} for r in state["pve_roles"]]))
        return 0
    if args[:2] == ["user", "add"]:
        if _fail("pveum_user_add"):
            return 1
        state["pve_users"].append(args[2])
        _save_state(state)
        return 0
    if args[:2] == ["role", "add"]:
        if _fail("pveum_role_add"):
            return 1
        state["pve_roles"].append(args[2])
        _save_state(state)
        return 0
    if args[:2] == ["acl", "modify"]:
        return 1 if _fail("pveum_acl_modify") else 0
    if args[:2] == ["acl", "delete"]:
        return 0
    if args[:3] == ["user", "token", "add"]:
        if _fail("pveum_token_add"):
            return 1
        token_id = args[3]
        state["pve_tokens"].append(f"{args[2]}!{token_id}")
        _save_state(state)
        print(json.dumps({
            "full-tokenid": f"{args[2]}!{token_id}",
            "info": {"privsep": "1"},
            "value": SCENARIO.get("pve_token_secret", "00000000-0000-0000-0000-000000000000"),
        }))
        return 0
    if args[:3] == ["user", "token", "permissions"]:
        perms = SCENARIO.get("token_permissions", {"Sys.Audit": 1, "VM.Audit": 1})
        print(json.dumps(perms))
        return 0
    if args[:3] == ["user", "token", "remove"]:
        return 0
    if args[:2] == ["role", "delete"]:
        return 0
    if args[:2] == ["user", "delete"]:
        return 0
    if args[:2] == ["user", "permissions"]:
        print(json.dumps(SCENARIO.get("token_permissions", {"Sys.Audit": 1, "VM.Audit": 1})))
        return 0
    _log("pveum", "UNHANDLED", *args)
    return 2


def cmd_pveam(args):
    _log("pveam", *args)
    if args[:1] == ["update"]:
        return 0
    if args[:1] == ["list"]:
        for entry in SCENARIO.get("local_templates", [FAKE_TEMPLATE_VOLID]):
            print(entry)
        return 0
    if args[:1] == ["available"]:
        for entry in SCENARIO.get("available_templates", [f"system {FAKE_TEMPLATE_FILENAME}"]):
            print(entry)
        return 0
    if args[:1] == ["download"]:
        return 1 if _fail("pveam_download") else 0
    return 2


def cmd_pvesh(args):
    _log("pvesh", *args)
    if args[:1] == ["get"] and "/network" in args[1]:
        bridges = SCENARIO.get("bridges", [FAKE_BRIDGE])
        print(json.dumps([{"iface": b, "type": "bridge"} for b in bridges]))
        return 0
    return 2


def cmd_pvesm(args):
    _log("pvesm", *args)
    if args[:1] == ["status"]:
        if "--storage" in args:
            name = args[args.index("--storage") + 1]
            avail = SCENARIO.get("storage_available_bytes", 100 * 1024 * 1024 * 1024)
            print("Name Type Status Total Used Available %")
            print(f"{name} dir active 200000000000 1000000000 {avail} 1.00")
            return 0
        content = args[args.index("--content") + 1] if "--content" in args else "rootdir"
        storages = SCENARIO.get("storages", {}).get(content, [FAKE_STORAGE])
        print("Name Type Status Total Used Available %")
        for name in storages:
            print(f"{name} dir active 200000000000 1000000000 100000000000 1.00")
        return 0
    return 2


def cmd_nft(args):
    _log("nft", *args)
    return 2


DISPATCH = {
    "pct": cmd_pct,
    "pveum": cmd_pveum,
    "pveam": cmd_pveam,
    "pvesh": cmd_pvesh,
    "pvesm": cmd_pvesm,
}


def main():
    name = os.environ["HUBINET_FAKE_COMMAND_NAME"]
    handler = DISPATCH.get(name)
    if handler is None:
        sys.exit(127)
    sys.exit(handler(sys.argv[1:]))


if __name__ == "__main__":
    main()
'''


@dataclass
class FakePveEnvironment:
    bin_dir: Path
    ct_root: Path
    log_path: Path
    scenario_path: Path
    state_path: Path
    env: dict[str, str] = field(default_factory=dict)

    def log_lines(self) -> list[str]:
        if not self.log_path.exists():
            return []
        return self.log_path.read_text(encoding="utf-8").splitlines()

    def ct_file(self, vmid: str, ct_path: str) -> Path:
        return self.ct_root / vmid / ct_path.lstrip("/")

    def ct_file_text(self, vmid: str, ct_path: str) -> str:
        return self.ct_file(vmid, ct_path).read_text(encoding="utf-8")


def default_scenario() -> dict[str, Any]:
    return {
        "fail": [],
        "ct_ip": FAKE_CT_IP,
        "local_templates": [FAKE_TEMPLATE_VOLID],
        "available_templates": [f"system {FAKE_TEMPLATE_FILENAME}"],
        "bridges": [FAKE_BRIDGE],
        "storages": {"rootdir": [FAKE_STORAGE], "vztmpl": [FAKE_TEMPLATE_STORAGE]},
        "storage_available_bytes": 100 * 1024 * 1024 * 1024,
        "token_permissions": {"Sys.Audit": 1, "VM.Audit": 1},
        "pve_token_secret": FAKE_PVE_TOKEN_SECRET,
        "r0_api_token": "f" * 64,
        "systemd_status": "running",
        "failed_units": [],
        "legacy_present": {},
        "health_body": '{"status": "ok"}',
        "snapshot_body": '{"sources": [{"display_name": "Home Proxmox"}], "resources": []}',
    }


_FAKE_COMMANDS = ("pct", "pveum", "pveam", "pvesh", "pvesm", "nft")


def build_minimal_source_checkout(tmp_path: Path, repo_root: Path) -> Path:
    """A tiny, fast, git-initialized stand-in for --source-dir.

    Tests must not depend on the real developer working tree's incidental
    contents (leftover local `.pytest-tmp-*`/`.tmp-pytest-*` scratch
    directories are common and can be very large) or on its own git
    history. This copies only the exact files phase 8 actually reads
    (deploy/install-0.5.0-fresh.sh itself is intentionally NOT copied
    here for most tests -- the fake `pct exec ... bash .../install-0.5.0-
    fresh.sh` step is scenario-driven and never executes real content),
    then `git init`s and commits it so `git archive HEAD` behaves exactly
    like it would against a real release checkout.
    """
    src = tmp_path / "source-checkout"
    (src / "app").mkdir(parents=True)
    (src / "deploy").mkdir(parents=True)
    (src / "config").mkdir(parents=True)
    (src / "app" / "__init__.py").write_text("", encoding="utf-8")
    (src / "requirements.txt").write_text("fastapi==0.116.1\n", encoding="utf-8")
    (src / "deploy" / "install-0.5.0-fresh.sh").write_text(
        (repo_root / "deploy" / "install-0.5.0-fresh.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (src / "config" / "inventory.example.yaml").write_text(
        "source:\n  display_name: example\n", encoding="utf-8"
    )

    import subprocess as _subprocess

    for cmd in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "test@example.invalid"],
        ["git", "config", "user.name", "test"],
        ["git", "add", "-A"],
        ["git", "commit", "-q", "-m", "fixture"],
    ):
        _subprocess.run(cmd, cwd=str(src), check=True, capture_output=True)

    return src


def build_fake_pve_environment(tmp_path: Path, scenario: dict[str, Any] | None = None) -> FakePveEnvironment:
    scenario = scenario if scenario is not None else default_scenario()

    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    ct_root = tmp_path / "ctroot"
    ct_root.mkdir(exist_ok=True)
    log_path = tmp_path / "fake-command.log"
    scenario_path = tmp_path / "scenario.json"
    state_path = tmp_path / "state.json"
    dispatcher_path = tmp_path / "_dispatcher.py"

    scenario_path.write_text(json.dumps(scenario))
    state_path.write_text(json.dumps({"vmids": {}, "pve_users": [], "pve_roles": [], "pve_tokens": []}))
    dispatcher_path.write_text(_DISPATCHER_SOURCE, encoding="utf-8")

    python_exe = sys.executable

    for name in _FAKE_COMMANDS:
        shim = bin_dir / name
        shim.write_text(
            textwrap.dedent(f"""\
                #!/usr/bin/env bash
                export HUBINET_FAKE_COMMAND_NAME="{name}"
                exec "{python_exe}" "{dispatcher_path}" "$@"
                """),
            encoding="utf-8",
        )
        shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["HUBINET_FAKE_LOG"] = str(log_path)
    env["HUBINET_FAKE_SCENARIO"] = str(scenario_path)
    env["HUBINET_FAKE_CT_ROOT"] = str(ct_root)
    env["HUBINET_FAKE_STATE"] = str(state_path)
    env["BOOTSTRAP_TEST_MODE"] = "1"
    # Windows Git-Bash-only: MSYS auto-converts POSIX-looking absolute-path
    # arguments (e.g. "/etc/hubinet-ops/...") into Windows paths when
    # exec'ing a native (non-MSYS) executable such as python.exe. That
    # conversion doesn't exist on Linux/macOS and has nothing to do with
    # the bootstrap script's own logic -- it only affects how this test
    # harness's fake-command shims hand arguments to the Python dispatcher
    # on a Windows dev machine. Harmless no-op on Linux CI.
    # Deliberately NOT set here (would also affect `git`, whose own -C/-o
    # arguments need normal MSYS path conversion to work at all) -- see
    # the per-shim MSYS2_ARG_CONV_EXCL scoping below instead.
    # Fast, bounded timeouts for tests -- the fake commands respond
    # instantly, so a short bound still exercises the real polling-loop
    # code path without slowing the suite down.
    env.setdefault("BOOTSTRAP_NET_TIMEOUT_SECONDS", "2")
    env.setdefault("BOOTSTRAP_SERVICE_TIMEOUT_SECONDS", "2")

    return FakePveEnvironment(
        bin_dir=bin_dir,
        ct_root=ct_root,
        log_path=log_path,
        scenario_path=scenario_path,
        state_path=state_path,
        env=env,
    )
