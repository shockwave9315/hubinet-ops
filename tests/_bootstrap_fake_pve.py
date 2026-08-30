"""Hermetic fake PVE command layer for testing deploy/bootstrap-proxmox-0.5.sh.

Not a test file itself (no test_ functions) -- a fixture/helper module used
by tests/test_bootstrap_proxmox_0_5.py (local-safe, static-only) and
tests/test_bootstrap_proxmox_0_5_smoke.py (sandbox-only, executes the real
script -- see that file's own module docstring for why it is gated).

Per AGENTS.md's test-boundary rules, tests must never invoke real `pct`,
`pveum`, `pveam`, `pvesh`, `pvesm`, `nft`, or contact any real network/PVE
endpoint. This module builds a temporary PATH containing fake replacements
for exactly those command names, host architecture detection via `dpkg`, plus
the CT-side commands the bootstrap script now also depends on (`apt-get`,
`env`, `sh -c "command -v ..."`, and
a simulated `python3 .../hubinet-ops-bootstrap-accept.py` discovery-
acceptance check) -- nothing else on PATH is touched (real `bash`, `awk`,
`grep`, `sed`, `git`, `tar`, `python3` itself, etc. remain the real system
binaries; none of those contact PVE or a private network on their own, and
`git` in particular is deliberately real -- see build_minimal_source_checkout
-- because Mandatory Fix 4/6 (source-provenance gating) is specifically
about real git behavior).

Effective-permission verification is derived from actually-recorded ACL
grants and role privilege sets in the fake's own state, not returned from a
fixed canned value keyed only by scenario -- a test exercising "missing
required privilege" or "extra mutation privilege" must actually cause the
fake's ACL/role state to reflect that (via `role_privs_override`, below),
so the fake command layer cannot silently diverge from the real sequence of
`pveum acl modify`/`pveum role add` calls the script under test issued.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import textwrap
from typing import Any

FAKE_PVE_TOKEN_SECRET = "00000000-0000-0000-0000-000000000000"
FAKE_HA_SOURCE_CIDR = "192.0.2.50/32"
# Real-PVE corrective note (sixth pass): the canonical `ip saddr`
# expression text real nftables reports for FAKE_HA_SOURCE_CIDR -- since
# it's a /32, that's the bare address with no prefix suffix (see
# deploy/lib/bootstrap-firewall.sh::_nft_canonical_ha_source_expr).
# Production generation now writes this canonical form directly (not the
# operator's literal --ha-source text), so tests asserting against
# GENERATED ruleset content (the pushed file, not only the simulated
# `nft list ruleset` round trip) must expect this, not FAKE_HA_SOURCE_CIDR
# itself.
FAKE_HA_SOURCE_CANONICAL = "192.0.2.50"
FAKE_PVE_ENDPOINT_HOST = "192.0.2.10"
FAKE_PVE_ENDPOINT = f"https://{FAKE_PVE_ENDPOINT_HOST}:8006"
FAKE_CT_IP = "192.0.2.200"
FAKE_BRIDGE = "vmbr0"
FAKE_STORAGE = "local-lxc"
FAKE_TEMPLATE_STORAGE = "local"
FAKE_TEMPLATE_FILENAME = "debian-13-standard_13.6-1_amd64.tar.zst"
FAKE_TEMPLATE_VOLID = f"{FAKE_TEMPLATE_STORAGE}:vztmpl/{FAKE_TEMPLATE_FILENAME}"
FAKE_NEXT_VMID = "110"
FAKE_R0_API_TOKEN = "f" * 64
FAKE_DISPLAY_NAME = "Home Proxmox"
# Real-PVE corrective note (sixth pass): the deterministic UID this
# fake's simulated `hubinetops` account holds -- `id -u hubinetops`
# reports it, and the simulated `nft list ruleset` round trip reports it
# in place of the symbolic `meta skuid "hubinetops"` name, matching what
# a real dogfood run observed. Kept in this outer module too (not only in
# the embedded dispatcher source, which cannot see this module's own
# names) so default_scenario() and tests can both reference it by name
# instead of a repeated magic literal.
FAKE_HUBINETOPS_UID = "999"

_DISPATCHER_SOURCE = r'''
import ipaddress
import json
import os
import re
import signal
import shutil
import sys
import tarfile
from pathlib import Path

FAKE_STORAGE = "local-lxc"
FAKE_TEMPLATE_STORAGE = "local"
FAKE_TEMPLATE_FILENAME = "debian-13-standard_13.6-1_amd64.tar.zst"
FAKE_TEMPLATE_VOLID = f"{FAKE_TEMPLATE_STORAGE}:vztmpl/{FAKE_TEMPLATE_FILENAME}"
FAKE_BRIDGE = "vmbr0"
FAKE_NEXT_VMID = "110"
FAKE_HUBINETOPS_UID_DEFAULT = "999"

LOG = Path(os.environ["HUBINET_FAKE_LOG"])
SCENARIO = json.loads(Path(os.environ["HUBINET_FAKE_SCENARIO"]).read_text())
CT_ROOT = Path(os.environ["HUBINET_FAKE_CT_ROOT"])
STATE_PATH = Path(os.environ["HUBINET_FAKE_STATE"])


def _default_state():
    return {
        "vmids": {},
        "pve_users": {},  # userid -> {"comment": str}
        "pve_roles": {},  # rolename -> [privs...]
        "pve_tokens": {},  # "user!tokenid" -> {"comment": str}
        "acl_grants": [],  # [{"target": "user:X"|"token:Y", "role": rolename}]
        "nextid_call_count": 0,
        "pveum_user_list_calls": 0,
    }


def _load_state():
    if STATE_PATH.exists():
        state = json.loads(STATE_PATH.read_text())
        for key, value in _default_state().items():
            state.setdefault(key, value)
        return state
    return _default_state()


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
    "opt/hubinet-ops",
    "tmp/hubinet-ops-src",
    "tmp/nftables.conf",
    "tmp/hubinet-ops-src.tar.gz",
    "tmp/hubinet-ops-bootstrap-accept.py",
    "tmp/hubinet-ops-update-src",
    "tmp/hubinet-ops-update-src.tar.gz",
    "tmp/hubinet-ops-authority-tool",  # matches both the fixed and the
    "tmp/hubinet-ops-update-probe",    # run-id-suffixed pushed path shape
    "tmp/hubinet-ops-update-venv-stage.py",
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
        template = args[2]
        match = re.search(r"_([^_]+)\.tar\.(?:gz|xz|zst)$", template)
        entry = {
            "started": False,
            "onboot": "0",
            "features": "",
            "template": template,
            "arch": match.group(1) if match else "unknown",
        }
        # --nameserver (P2-2 third pass): bootstrap-container.sh passes this
        # only in hostname PVE endpoint mode, carrying --dns-resolver's
        # value -- persisted here exactly as a real PVE host would persist
        # it into the container's own config, so `pct config`/the
        # simulated /etc/resolv.conf regeneration on `pct start` below can
        # reflect it.
        if "--nameserver" in args:
            entry["nameserver"] = args[args.index("--nameserver") + 1]
        state["vmids"][vmid] = entry
        _save_state(state)
        sys.exit(0)

    if sub == "config":
        vmid = args[1]
        if _fail("pct_config"):
            sys.exit(1)
        entry = state["vmids"].get(vmid, {})
        lines = [f"arch: {entry.get('arch', 'unknown')}", "hostname: hubinet-ops"]
        if entry.get("nameserver"):
            lines.append(f"nameserver: {entry['nameserver']}")
        sys.stdout.write("\n".join(lines) + "\n")
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
        entry = state["vmids"].setdefault(vmid, {"started": False, "onboot": "0", "features": ""})
        host_arch = SCENARIO.get("host_debian_arch", "amd64")
        if entry.get("arch") != host_arch:
            sys.exit(1)
        entry["started"] = True
        _save_state(state)
        # Simulate PVE's own real container-start machinery regenerating
        # /etc/resolv.conf inside the guest from the persisted `nameserver`
        # config key (declared_dns_resolver, P2-2 third pass) -- read back
        # by bootstrap-firewall.sh's _verify_ct_dns_resolver_matches_declared
        # before the firewall is generated. "ct_actual_resolv_conf" lets a
        # test simulate a genuine declared-vs-actual mismatch (an
        # out-of-band operator change, a template using a stub resolver
        # such as systemd-resolved, or simply "no nameserver at all")
        # without having to fake PVE's real regeneration logic exactly.
        resolv_override = SCENARIO.get("ct_actual_resolv_conf")
        resolv_path = _ct_path(vmid, "/etc/resolv.conf")
        if resolv_override is False:
            pass  # simulates an unreadable/missing /etc/resolv.conf
        elif resolv_override is not None:
            resolv_path.parent.mkdir(parents=True, exist_ok=True)
            resolv_path.write_text(resolv_override)
        elif entry.get("nameserver"):
            resolv_path.parent.mkdir(parents=True, exist_ok=True)
            resolv_path.write_text(f"nameserver {entry['nameserver']}\n")
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
        # "pct_push_fail_dest_suffixes": [...] -- fail only the pushes
        # whose CT destination ends with one of these suffixes. A blanket
        # "fail every push" cannot express "this ONE staged artifact could
        # not be transferred", which is what a Phase U3 staging-failure
        # regression needs (every other push in the same run must still
        # succeed so the run actually reaches staging).
        suffixes = SCENARIO.get("pct_push_fail_dest_suffixes", [])
        if isinstance(suffixes, str):
            suffixes = [suffixes]
        if any(_normalize_ct_arg(ct_path).endswith(suffix) for suffix in suffixes):
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


# Deterministic fake failure seams for the in-place updater's individual
# CT-side mv/cp activation steps (deploy/lib/update-activate.sh) -- keyed
# by the LOGICAL (run-id-independent) source/destination shape of each
# move, so a test can force exactly one intermediate activation step to
# fail without knowing UPDATE_RUN_ID (random per invocation) in advance.
# ("exact", value) matches a normalized CT path exactly; ("prefix", value)
# matches anything starting with value (used for the run-id-suffixed
# staged-/rollback- side of each move).
_ACTIVATION_MOVE_FAIL_RULES = [
    ("mv", ("exact", "/opt/hubinet-ops/app"), ("prefix", "/opt/hubinet-ops/app.rollback-"), "mv_live_app_to_rollback"),
    ("mv", ("prefix", "/opt/hubinet-ops/app.staged-"), ("exact", "/opt/hubinet-ops/app"), "mv_staged_app_to_live"),
    ("mv", ("exact", "/opt/hubinet-ops/.venv"), ("prefix", "/opt/hubinet-ops/.venv.rollback-"), "mv_live_venv_to_rollback"),
    ("mv", ("prefix", "/opt/hubinet-ops/.venv.staged-"), ("exact", "/opt/hubinet-ops/.venv"), "mv_staged_venv_to_live"),
    ("mv", ("exact", "/opt/hubinet-ops/requirements.txt"), ("prefix", "/opt/hubinet-ops/requirements.txt.rollback-"), "mv_live_requirements_to_rollback"),
    ("mv", ("prefix", "/opt/hubinet-ops/requirements.txt.staged-"), ("exact", "/opt/hubinet-ops/requirements.txt"), "mv_staged_requirements_to_live"),
    ("cp", ("exact", "/etc/systemd/system/hubinet-ops.service"), ("prefix", "/etc/systemd/system/hubinet-ops.service.rollback-"), "cp_live_unit_to_rollback"),
    ("mv", ("prefix", "/etc/systemd/system/hubinet-ops.service.staged-"), ("exact", "/etc/systemd/system/hubinet-ops.service"), "mv_staged_unit_to_live"),
    # ROLLBACK-side restore of the preserved old unit. Needed as its own
    # seam (correction pass 8, P2) so a test can interrupt the updater
    # exactly BETWEEN "the old unit file is back on the live path" and
    # "systemd has been told about it", the reachable state in which a
    # replayed rollback must still daemon-reload.
    ("mv", ("prefix", "/etc/systemd/system/hubinet-ops.service.rollback-"), ("exact", "/etc/systemd/system/hubinet-ops.service"), "mv_rollback_unit_to_live"),
    ("mv", ("exact", "/opt/hubinet-ops/.hubinet-source-commit"), ("prefix", "/opt/hubinet-ops/.hubinet-source-commit.rollback-"), "mv_live_marker_to_rollback"),
    ("mv", ("prefix", "/opt/hubinet-ops/.hubinet-source-commit.staged-"), ("exact", "/opt/hubinet-ops/.hubinet-source-commit"), "mv_staged_marker_to_live"),
]


def _match_endpoint(mode_value, normalized_path):
    mode, value = mode_value
    return normalized_path == value if mode == "exact" else normalized_path.startswith(value)


def _activation_fail_key(op, src_norm, dst_norm):
    for rule_op, src_rule, dst_rule, key in _ACTIVATION_MOVE_FAIL_RULES:
        if rule_op == op and _match_endpoint(src_rule, src_norm) and _match_endpoint(dst_rule, dst_norm):
            return key
    return None


_ROLLBACK_REMOVE_KEYS = {
    "/opt/hubinet-ops/app": "rm_live_app",
    "/opt/hubinet-ops/.venv": "rm_live_venv",
    "/opt/hubinet-ops/requirements.txt": "rm_live_requirements",
    "/etc/systemd/system/hubinet-ops.service": "rm_live_unit",
    "/opt/hubinet-ops/.hubinet-source-commit": "rm_live_marker",
}


def _matches_run_owned_ct_script(raw_arg, base_name):
    # deploy/lib/update-plan.sh (P2-A/small-cleanup, AGENTS.md) now pushes
    # this planning tool to a run-id-suffixed path
    # ("<base_name>-<UPDATE_RUN_ID>.py"), never the fixed "<base_name>.py"
    # name -- match either shape so this fake dispatcher recognizes it
    # regardless of which naming convention produced the pushed path.
    normalized = raw_arg.replace("\\", "/")
    return bool(re.search(rf"(^|/){re.escape(base_name)}(-[0-9a-f]+)?\.py$", normalized))


# Correction pass 7 (P1 test realism): a simulated
# `pct exec <vmid> -- python3 <ct-script-path> ...` must never INVENT the
# execution of a file that does not exist in this fake CT filesystem.
# Matching the argv shape alone hid a real defect: the run-owned authority
# helper lives in the container's volatile /tmp, and a genuine PVE/CT
# reboot clears it, so "the updater re-pushed what it needs" and "the
# updater silently depended on a file that is gone" were indistinguishable
# in this fake. These two helpers make the fake fail exactly like real
# python3 does instead.
def _pushed_ct_script_exists(vmid, raw_arg):
    return _ct_path(vmid, raw_arg).is_file()


def _missing_python_script(raw_arg):
    normalized = _normalize_ct_arg(raw_arg)
    _log("pct", "exec", "python3", "MISSING-SCRIPT", normalized)
    sys.stderr.write(
        f"python3: can't open file '{normalized}': [Errno 2] No such file or directory\n"
    )
    # Real CPython's own exit status for an unopenable script argument.
    return 2


def _exec_inner(vmid, inner, state):
    joined = " ".join(inner)
    ct = str(vmid)

    if inner[:2] == ["mkdir", "-p"]:
        _ct_path(vmid, inner[2]).mkdir(parents=True, exist_ok=True)
        return 0

    if inner[0] == "install" and "-d" in inner:
        _ct_path(vmid, inner[-1]).mkdir(parents=True, exist_ok=True)
        return 0

    if inner[0] == "ssh-keygen":
        key_path = inner[inner.index("-f") + 1]
        comment = inner[inner.index("-C") + 1]
        private = _ct_path(vmid, key_path)
        private.parent.mkdir(parents=True, exist_ok=True)
        private.write_text("FAKE PRIVATE KEY FOR SANDBOX TEST ONLY\n", encoding="utf-8")
        private.with_name(private.name + ".pub").write_text(
            f"ssh-ed25519 QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE= {comment}\n",
            encoding="utf-8",
        )
        return 0

    if inner[0] == "runuser" and "ssh" in inner:
        sys.stdout.write(
            '{"response_version":1,"ok":false,"context":{},'
            '"error":{"classification":"execution_failed",'
            '"message":"unknown host-control operation"}}'
        )
        return 2

    if inner[0] == "tar" and "-xzf" in inner:
        # The in-place updater's own staging step (unlike the bootstrap
        # installer, which never inspects extracted content) needs this
        # extraction to be real: later fake commands (cp -a, cat, ...)
        # read the extracted files back. Still fully hermetic --
        # Python's own stdlib tarfile against a tarball `pct push` already
        # copied for real onto this same simulated CT filesystem.
        if _fail("tar_extract"):
            return 1
        tarball_arg = inner[inner.index("-xzf") + 1]
        dest_arg = inner[inner.index("-C") + 1] if "-C" in inner else "."
        tarball_path = _ct_path(vmid, tarball_arg)
        dest_path = _ct_path(vmid, dest_arg)
        dest_path.mkdir(parents=True, exist_ok=True)
        with tarfile.open(tarball_path, "r:gz") as archive:
            archive.extractall(dest_path, filter="data")  # noqa: S202 -- trusted, test-only tarball
        return 0

    if inner[0] == "tar":
        return 0

    if inner[0] == "rm":
        # Generalized (beyond the original host-control-only special
        # case): the in-place updater's own rollback/cleanup steps
        # `rm -rf` several different staged/rollback paths, and a later
        # `mv` into a path this didn't actually remove would otherwise
        # nest incorrectly (Python's shutil.move moves INTO an existing
        # directory rather than replacing it) -- so this fake must really
        # remove each named target, not merely report success.
        targets = [a for a in inner[1:] if not a.startswith("-")]
        for raw_target in targets:
            normalized_target = _normalize_ct_arg(raw_target)
            target_path = _ct_path(vmid, normalized_target)
            remove_key = _ROLLBACK_REMOVE_KEYS.get(normalized_target)
            if remove_key is not None and _fail(f"{remove_key}_partial"):
                # Realistic mutate-then-fail: remove some content while
                # leaving the load-bearing destination path itself in
                # place. A subsequent directory `mv` could otherwise
                # nest its source inside this surviving directory.
                if target_path.is_dir():
                    children = sorted(target_path.iterdir(), key=lambda item: item.name)
                    if children:
                        child = children[0]
                        if child.is_dir():
                            shutil.rmtree(child)
                        else:
                            child.unlink()
                elif target_path.exists():
                    target_path.write_bytes(b"partial")
                return 1
            if remove_key is not None and _fail(f"{remove_key}_noop_success"):
                # The command reports success but the path remains. The
                # caller must trust an independent postcondition probe,
                # never this return code.
                continue
            if target_path.is_dir():
                shutil.rmtree(target_path, ignore_errors=True)
            elif target_path.exists():
                target_path.unlink()
        return 0

    if inner[0] == "mv" and len(inner) >= 3:
        src_norm = _normalize_ct_arg(inner[-2])
        dst_norm = _normalize_ct_arg(inner[-1])
        fail_key = _activation_fail_key("mv", src_norm, dst_norm)
        if fail_key is not None and _fail(fail_key):
            # Simulates the realistic failure shape: the command fails
            # before mutating anything (a real rename() is atomic --
            # either it fully happens or nothing does), so neither src
            # nor dst is touched.
            return 1
        src = _ct_path(vmid, inner[-2])
        dst = _ct_path(vmid, inner[-1])
        if not src.exists():
            return 1
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists() and dst.is_dir() and not src.is_dir():
            return 1
        shutil.move(str(src), str(dst))
        if fail_key is not None and SCENARIO.get("kill_updater_after_move") == fail_key:
            # Actual untrappable updater disappearance: the fake command
            # has completed the selected atomic rename, then SIGKILLs its
            # parent updater shell. No EXIT trap or rollback code runs.
            os.kill(os.getppid(), signal.SIGKILL)
        return 0

    if inner[0] == "cp":
        cp_args = [a for a in inner[1:] if not a.startswith("-")]
        if len(cp_args) != 2:
            return 2
        src_norm = _normalize_ct_arg(cp_args[0])
        dst_norm = _normalize_ct_arg(cp_args[1])
        fail_key = _activation_fail_key("cp", src_norm, dst_norm)
        # "<fail_key>_partial" (P1-B correction pass 2): simulates a
        # realistic NON-atomic `cp` failure (ENOSPC/EIO/etc partway
        # through the write) -- unlike the ordinary fail_key case below
        # (the command fails before creating any destination at all,
        # simulating a `cp` that never started writing), this leaves a
        # PARTIAL destination file behind. Proves a caller never treats
        # mere existence of a rollback-copy destination as complete,
        # trustworthy pre-update state.
        if fail_key is not None and _fail(f"{fail_key}_partial"):
            src = _ct_path(vmid, cp_args[0])
            dst = _ct_path(vmid, cp_args[1])
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.exists() and src.is_file():
                content = src.read_bytes()
                dst.write_bytes(content[: max(1, len(content) // 2)])
            else:
                dst.write_bytes(b"partial")
            return 1
        if fail_key is not None and _fail(fail_key):
            return 1
        src = _ct_path(vmid, cp_args[0])
        dst = _ct_path(vmid, cp_args[1])
        if not src.exists():
            return 1
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(str(src), str(dst), dirs_exist_ok=True)
        else:
            shutil.copyfile(str(src), str(dst))
        return 0

    if inner[0] == "chown":
        if (
            _normalize_ct_arg(inner[-1]) == "/var/lib/hubinet-ops/authority.db"
            and _fail("authority_restore_chown")
        ):
            return 1
        return 0

    if inner[0] == "chmod":
        if (
            _normalize_ct_arg(inner[-1]) == "/var/lib/hubinet-ops/authority.db"
            and _fail("authority_restore_chmod")
        ):
            return 1
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

    if inner[0] == "id" and inner[1:] == ["-u", "hubinetops"]:
        # Real-PVE corrective note (sixth pass): bootstrap-firewall.sh's
        # _hubinetops_uid derives this exact command's output as the
        # numeric UID real nftables reports back for `meta skuid
        # "hubinetops"` on the active-ruleset round trip (see
        # _canonicalize_active_nft_text below) -- never hardcoded, always
        # read back from the target container. "id_u_hubinetops" in the
        # "fail" list simulates the command itself failing (e.g. the user
        # somehow doesn't exist yet); "hubinetops_uid_malformed" simulates
        # a genuinely unparseable result.
        if _fail("id_u_hubinetops"):
            return 1
        if SCENARIO.get("hubinetops_uid_malformed"):
            sys.stdout.write("not-a-uid\n")
            return 0
        sys.stdout.write(SCENARIO.get("hubinetops_uid", FAKE_HUBINETOPS_UID_DEFAULT) + "\n")
        return 0

    if inner[0] == "apt-get":
        return _exec_apt_get(inner[1:], state)

    if inner[0] == "env":
        # `env VAR=val [VAR2=val2 ...] <command> [args...]` -- strip the
        # leading VAR=val assignments and dispatch the wrapped command;
        # none of these assignments are secret.
        idx = 1
        while idx < len(inner) and "=" in inner[idx] and not inner[idx].startswith("-"):
            idx += 1
        return _exec_inner(vmid, inner[idx:], state)

    if inner[:2] == ["sh", "-c"]:
        return _exec_sh_c(inner[2])

    if inner[0] == "python3" and inner[1].replace("\\", "/").endswith("hubinet-ops-bootstrap-accept.py"):
        if not _pushed_ct_script_exists(vmid, inner[1]):
            return _missing_python_script(inner[1])
        return _exec_discovery_accept(vmid, inner[2:])

    if inner[0] == "python3" and inner[1].replace("\\", "/").endswith("hubinet-ops-bootstrap-resolve-dns.py"):
        if not _pushed_ct_script_exists(vmid, inner[1]):
            return _missing_python_script(inner[1])
        return _exec_resolve_dns(inner[2:])

    if inner[0] == "python3" and _matches_run_owned_ct_script(inner[1], "hubinet-ops-authority-tool"):
        if not _pushed_ct_script_exists(vmid, inner[1]):
            return _missing_python_script(inner[1])
        return _exec_authority_tool(vmid, inner[2:], state)

    if inner[0] == "python3" and _matches_run_owned_ct_script(inner[1], "hubinet-ops-update-probe"):
        if not _pushed_ct_script_exists(vmid, inner[1]):
            return _missing_python_script(inner[1])
        return _exec_update_probe(vmid)

    if inner[0] == "python3" and inner[1].replace("\\", "/").endswith("hubinet-ops-update-venv-stage.py"):
        if not _pushed_ct_script_exists(vmid, inner[1]):
            return _missing_python_script(inner[1])
        return _exec_venv_stage(vmid, inner[2:])

    _log("pct", "exec", "UNHANDLED", *inner)
    return 2


def _exec_authority_tool(vmid, args, state):
    # Simulates deploy/lib/hubinet-ops-authority-tool.py's OWN observable
    # JSON contract without a real sqlite database -- a fake "authority.db"
    # is simply a small JSON object {"marker", "schema_version",
    # "backend_instance_id"} written directly at the CT path by whatever
    # seeded/activated it (see seed_authority_db / the real production
    # code path, both of which write through this same simulated
    # filesystem). This keeps the shell-level orchestration tests
    # (staging/activation/rollback order, ledger gating) exercised
    # against realistic pass/fail outcomes while the REAL tool's own
    # sqlite/backup/integrity-check logic is covered by direct pytest
    # unit tests against the real script (no fake needed there).
    if not args:
        return 2
    subcommand = args[0]
    if subcommand == "path-state" and len(args) == 2:
        normalized = _normalize_ct_arg(args[1])
        failure_prefixes = SCENARIO.get("path_probe_transport_fail_prefixes", [])
        if isinstance(failure_prefixes, str):
            failure_prefixes = [failure_prefixes]
        if _fail("path_probe_transport") or any(
            normalized.startswith(prefix) for prefix in failure_prefixes
        ):
            # Outer pct/transport failure: deliberately no JSON answer.
            return 1
        print(json.dumps({"ok": True, "exists": _ct_path(vmid, args[1]).exists()}))
        return 0
    if subcommand == "remove":
        # "fail_nth_authority_remove": N -- fails only the Nth call this
        # run makes to `remove` (1-indexed), succeeding on every other
        # call. Needed because update-activate.sh's forward reset path and
        # its own rollback-on-later-failure path both call `remove` with
        # IDENTICAL argv (the same db_path) -- a blanket "always fail
        # remove" scenario key cannot distinguish "fail the ORIGINAL
        # reset" from "fail removal of the newly-created target database
        # DURING rollback" (see AGENTS.md P1-B fail-closed-rollback
        # regression), so this counts real calls instead.
        call_number = state.get("authority_tool_remove_calls", 0) + 1
        state["authority_tool_remove_calls"] = call_number
        _save_state(state)
        fail_nth = SCENARIO.get("fail_nth_authority_remove")
        if fail_nth is not None and call_number == int(fail_nth):
            print(json.dumps({"ok": False, "reason": "simulated_remove_failure"}))
            return 1
        # "fail_nth_authority_remove_partial": N (P1-A correction pass 2)
        # -- simulates the REAL cmd_remove()'s own intermediate-unlink-
        # failure shape: its sequential unlink loop (db, then -wal, then
        # -shm) can succeed on an earlier path and only fail on a LATER
        # one, so the call as a whole still reports "ok": false having
        # already mutated live state. This fake has no real wal/shm
        # sidecars to partially remove (the whole fake "db" is one JSON
        # blob), so it approximates the same observable shape directly:
        # the underlying db path IS actually removed, but "ok": false is
        # still what the caller sees -- proving a caller that only arms
        # rollback after a fully successful `remove` would find no
        # marker recorded despite the live database already being gone.
        fail_nth_partial = SCENARIO.get("fail_nth_authority_remove_partial")
        if fail_nth_partial is not None and call_number == int(fail_nth_partial):
            target = _ct_path(vmid, args[1])
            if target.exists():
                target.unlink()
            print(json.dumps({"ok": False, "reason": "simulated_partial_remove_failure"}))
            return 1
    if _fail(f"authority_tool_{subcommand}"):
        print(json.dumps({"ok": False, "reason": "simulated_failure"}))
        return 1
    if subcommand == "inspect":
        path = _ct_path(vmid, args[1])
        if not path.exists() or path.stat().st_size == 0:
            print(json.dumps({"ok": False, "exists": False, "reason": "missing_or_empty"}))
            return 0
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            print(json.dumps({"ok": False, "exists": True, "reason": "structurally_unreadable"}))
            return 0
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("marker"), str)
            or not isinstance(data.get("schema_version"), int)
            or not isinstance(data.get("backend_instance_id"), str)
        ):
            print(json.dumps({"ok": False, "exists": True, "reason": "structurally_unreadable"}))
            return 0
        if data["marker"] != "hubinet_ops_0_5_authority":
            print(json.dumps({"ok": False, "exists": True, "reason": "marker_mismatch"}))
            return 0
        print(json.dumps({
            "ok": True, "exists": True, "marker": data["marker"],
            "schema_version": data["schema_version"],
            "backend_instance_id": data["backend_instance_id"],
            "schema_objects": data.get("schema_objects", []),
        }))
        return 0
    if subcommand == "backup" and len(args) == 6:
        db_arg, dest_arg, exp_marker, exp_version, exp_backend = args[1:6]
        src = _ct_path(vmid, db_arg)
        if not src.exists() or src.stat().st_size == 0:
            print(json.dumps({"ok": False, "reason": "live_recheck_missing_or_empty"}))
            return 1
        try:
            data = json.loads(src.read_text())
        except (OSError, ValueError):
            print(json.dumps({"ok": False, "reason": "live_recheck_structurally_unreadable"}))
            return 1
        if (
            data.get("marker") != exp_marker
            or str(data.get("schema_version")) != str(exp_version)
            or data.get("backend_instance_id") != exp_backend
        ):
            print(json.dumps({"ok": False, "reason": "live_recheck_context_changed"}))
            return 1
        dest = _ct_path(vmid, dest_arg)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(data), encoding="utf-8")
        print(json.dumps({
            "ok": True, "backup_path": dest_arg,
            "backend_instance_id": data["backend_instance_id"],
        }))
        return 0
    if subcommand == "remove" and len(args) == 2:
        target = _ct_path(vmid, args[1])
        if target.exists():
            target.unlink()
        print(json.dumps({"ok": True}))
        return 0
    print(json.dumps({"ok": False, "reason": "usage"}))
    return 2


def _exec_venv_stage(vmid, args):
    # Simulates deploy/lib/hubinet-ops-update-venv-stage.py's own
    # observable contract without invoking a real venv/pip anywhere.
    #
    # Correction pass 8 (P1): the destination is now the FINAL live venv
    # pathname, never a staging pathname that something later renames, so
    # this fake reproduces the two properties that correction depends on:
    #
    #   - the generated console entrypoint embeds the ABSOLUTE path of the
    #     environment it was built in (exactly why a real virtualenv is
    #     not relocatable), so an orchestration test can read the
    #     activated /opt/hubinet-ops/.venv/bin/pip back and see which
    #     pathname it was actually built at;
    #   - a failed build leaves a PARTIAL environment behind at that final
    #     pathname ("venv_create" fails after the directory exists but
    #     before pip; "pip_install" fails with the environment fully
    #     created), which is what rollback must remove and prove absent.
    #
    # "kill_updater_during_venv_build" additionally SIGKILLs the updater
    # once a partial environment exists at the final path -- the reboot /
    # untrappable-interruption witness for a final-path build.
    if len(args) != 2:
        return 2
    venv_path = _ct_path(vmid, args[0])
    if venv_path.exists():
        sys.stderr.write(f"refusing to build into an already-existing path: {args[0]}\n")
        return 1
    if not _ct_path(vmid, args[1]).is_file():
        sys.stderr.write(f"requirements file does not exist: {args[1]}\n")
        return 1
    (venv_path / "bin").mkdir(parents=True, exist_ok=True)
    if _fail("venv_create"):
        return 1
    (venv_path / "bin" / "pip").write_text(
        f"#!{_normalize_ct_arg(args[0])}/bin/python\n", encoding="utf-8"
    )
    (venv_path / "bin" / "python3").write_text("#!/bin/sh\n", encoding="utf-8")
    if SCENARIO.get("kill_updater_during_venv_build"):
        os.kill(os.getppid(), signal.SIGKILL)
    if _fail("pip_install"):
        return 1
    return 0


def _exec_update_probe(vmid):
    # Simulates deploy/lib/hubinet-ops-update-probe.py's own observable
    # JSON contract -- one bounded, non-waiting read of current state,
    # driven by "update_probe_*" scenario keys (default: mirrors the
    # discovery_* keys already used by _exec_discovery_accept so a test
    # scenario only needs to set one consistent set of facts for both the
    # pre-update probe and the post-update acceptance check).
    if _fail("update_probe"):
        print(json.dumps({"ok": False, "reason": "backend_endpoint_unreachable: simulated"}))
        return 0
    backend_id = SCENARIO.get(
        "update_probe_backend_instance_id",
        SCENARIO.get("discovery_backend_instance_id", "fake-backend-instance-id"),
    )
    sequence = SCENARIO.get("update_probe_sequence", SCENARIO.get("discovery_committed_sequence", 1))
    print(json.dumps({
        "ok": True,
        "backend_instance_id": backend_id,
        "service_active": True,
        "last_committed_run_sequence": sequence,
        "health": "healthy",
        "freshness": "fresh",
    }))
    return 0


def _exec_resolve_dns(args):
    # Simulates deploy/lib/hubinet-ops-bootstrap-resolve-dns.py's own
    # observable contract without performing a real DNS lookup -- driven
    # by the "dns_resolution" scenario key: {hostname: [ip, ...]}. A
    # hostname absent from that mapping, or mapped to an empty list,
    # simulates zero usable A records.
    host = args[0] if args else ""
    mapping = SCENARIO.get("dns_resolution", {})
    addresses = mapping.get(host)
    if not addresses:
        print(f"FAIL no-ipv4-addresses-resolved host={host!r}")
        return 1
    for ip in sorted(set(addresses)):
        print(ip)
    return 0


def _exec_apt_get(args, state=None):
    if _fail("apt_get"):
        if state is not None:
            _maybe_replace_identity_before_failure("apt_get", state)
        return 1
    return 0


def _maybe_replace_identity_before_failure(trigger, state):
    # P2-3 (third pass) witness: "Run A creates hubinetops@pve (ledger
    # records success); another administrator/process deletes it and
    # recreates a DIFFERENT hubinetops@pve; Run A later fails at some
    # unrelated later phase; rollback must prove the CURRENT object is
    # still Run A's own, not merely trust its own ledger." apt-get
    # (tooling provisioning, phase8b) is a realistic later-phase failure
    # point -- well after phase6 identity creation succeeded. Mutating
    # state here, immediately before returning the failure that will
    # eventually trigger rollback, simulates "the replacement already
    # happened by the time rollback's own live read-back runs" without
    # needing real concurrency.
    plan = SCENARIO.get("replace_identity_before_failure", {}).get(trigger)
    if not plan:
        return
    user_comment = plan.get("user_comment")
    if user_comment is not None:
        user = plan.get("user", "hubinetops@pve")
        if user in state["pve_users"]:
            state["pve_users"][user]["comment"] = user_comment
    token_comment = plan.get("token_comment")
    if token_comment is not None:
        full = plan.get("token", "hubinetops@pve!r0-readonly")
        if full in state["pve_tokens"]:
            state["pve_tokens"][full]["comment"] = token_comment
    # Tenth-pass corrective additions (P1 finding, independent review --
    # Codex): the "check every child token before deleting its parent
    # user" regression witness needs a REALISTIC way to have a token
    # OTHER than the expected r0-readonly genuinely present under
    # hubinetops@pve at rollback time (simulating a different
    # administrator or concurrent process registering it after this
    # run's own phase6 succeeded), and/or to have the expected token
    # itself genuinely gone already -- both via real state mutation, not
    # merely an output override that only fakes the listing command's
    # text.
    if plan.get("remove_expected_token"):
        state["pve_tokens"].pop(plan.get("expected_token", "hubinetops@pve!r0-readonly"), None)
    foreign_token = plan.get("add_foreign_token")
    if foreign_token:
        full = foreign_token.get("token", "hubinetops@pve!other-token")
        state["pve_tokens"][full] = {"comment": foreign_token.get("comment", "unrelated")}
    _save_state(state)


def _exec_sh_c(script):
    # Only form this fake needs to understand: `command -v <name>`, used by
    # bootstrap-deploy.sh's _require_ct_command after tooling provisioning.
    prefix = "command -v "
    if script.startswith(prefix):
        name = script[len(prefix):].strip()
        available = name in SCENARIO.get(
            "ct_tools_available", ["nft", "curl", "ss", "ssh", "ssh-keygen"]
        )
        return 0 if available else 1
    return 2


def _exec_discovery_accept(vmid, args):
    # Simulates deploy/lib/hubinet-ops-bootstrap-accept.py's OWN observable
    # contract (its stdout vocabulary and exit codes) without actually
    # running it against a real HTTP server -- there is no real
    # /etc/hubinet-ops/agent.env or listening backend inside this
    # simulated container filesystem. Driven by the "discovery_*" scenario
    # keys; see default_scenario() for the default (immediate healthy,
    # fresh, committed PASS with one node and zero resources, matching the
    # real script's own field vocabulary -- including the strengthened
    # committed-success proof: latest_completed_outcome, last_committed_
    # run_sequence, last_successful_observed_at, committed_context ==
    # current_context, and a non-empty nodes[]).
    #
    # The real hubinet-ops-bootstrap-accept.py connects to
    # http://127.0.0.1:8787 exactly like the process-health curl probe
    # above -- it is equally subject to this CT's own active firewall. If
    # the required loopback/reply semantics are absent, the real script's
    # very first call (GET /backend) would raise urllib.error.URLError and
    # print "FAIL backend-endpoint-unreachable <exc>"; this fake reproduces
    # that exact outcome rather than succeeding unconditionally.
    expected_name = args[0] if len(args) > 0 else ""
    result = SCENARIO.get("discovery_result", "healthy")
    backend_id = SCENARIO.get("discovery_backend_instance_id", "fake-backend-instance-id")
    source_name = SCENARIO.get("discovery_source_name", expected_name)
    resource_count = SCENARIO.get("discovery_resource_count", 0)
    node_count = SCENARIO.get("discovery_node_count", 1)
    committed_sequence = SCENARIO.get("discovery_committed_sequence", 1)
    # A 3rd positional argument mirrors the real script's own optional
    # min-committed-sequence-exclusive extension (see
    # deploy/lib/hubinet-ops-bootstrap-accept.py) -- used by the in-place
    # updater's post-update, DB-preserving acceptance check to prove a
    # genuine completed cycle happened AFTER the restart, not merely that
    # the pre-update state is still being reported.
    if len(args) >= 3:
        try:
            min_sequence_exclusive = int(args[2])
        except ValueError:
            min_sequence_exclusive = 0
        if min_sequence_exclusive and committed_sequence <= min_sequence_exclusive:
            print(
                f"FAIL committed-sequence-not-past-baseline "
                f"got={committed_sequence!r} baseline={min_sequence_exclusive!r}"
            )
            return 1

    state = _load_state()
    if not _firewall_permits_local_and_reply_traffic(vmid, state):
        print("FAIL backend-endpoint-unreachable simulated-firewall-blocked-loopback-or-reply")
        return 1

    if result == "backend_unreachable":
        print("FAIL backend-endpoint-unreachable simulated")
        return 1
    if not backend_id:
        print("FAIL backend-instance-id-missing-or-invalid")
        return 1
    print(f"INFO backend_instance_id={backend_id}")

    if result == "snapshot_unreachable":
        print("FAIL snapshot-endpoint-unreachable simulated")
        return 1
    if result == "source_count_mismatch":
        print("FAIL unexpected-source-count got=[]")
        return 1
    if source_name != expected_name:
        print(f"FAIL source-name-mismatch expected={expected_name!r} got={source_name!r}")
        return 1
    if isinstance(result, str) and result.startswith("terminal:"):
        health = result.split(":", 1)[1]
        print(f"FAIL source-health-terminal-failure health={health}")
        return 1
    if result == "timeout":
        print("FAIL discovery-timeout last_health=not_yet_observed")
        return 1
    if result == "healthy_but_stale":
        print("FAIL discovery-timeout last_health=healthy/stale")
        return 1
    if result == "unsuccessful_outcome":
        print("FAIL latest-completed-outcome-not-success got='partial'")
        return 1
    if result == "missing_committed_context":
        print("FAIL committed-context-missing")
        return 1
    if result == "context_mismatch":
        print("FAIL committed-current-context-mismatch field=source_config_revision committed=1 current=2")
        return 1
    if result == "zero_nodes":
        print("FAIL zero-nodes-after-healthy-commit got=[]")
        return 1
    if result == "resource_state_level_violation":
        print("FAIL resource-state-level-not-discovered got=managed")
        return 1
    if result == "resource_security_continuity_violation":
        print("FAIL resource-security-continuity-trusted resource=fake-resource-id")
        return 1
    if result == "resource_capabilities_violation":
        print("FAIL resource-has-effective-capabilities got=['snapshot.create']")
        return 1
    print(
        f"PASS backend_instance_id={backend_id} "
        f"source_health=healthy source_freshness=fresh "
        f"last_committed_run_sequence={committed_sequence} "
        f"node_count={node_count} resource_count={resource_count}"
    )
    return 0


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
    # Bare `enable`/`disable` (no --now) -- the in-place updater's temporary
    # service-autostart guard (deploy/lib/update-activate.sh). Real systemd
    # semantics: these only add/remove the boot-activation symlink; they
    # never start or stop the currently-running unit, and a disabled unit
    # can still be started by hand. The three seams below reproduce the
    # three realistic ways such a command lies about what it did.
    if args == ["disable", "hubinet-ops"]:
        call_number = state.get("service_autostart_disable_calls", 0) + 1
        state["service_autostart_disable_calls"] = call_number
        _save_state(state)
        if _fail("service_autostart_disable_mutate_then_fail"):
            # Boot activation IS actually removed, yet the command reports
            # failure. Recovery must already be armed at this point.
            entry["service_enabled"] = False
            _save_state(state)
            return 1
        if _fail("service_autostart_disable"):
            return 1
        if _fail("service_autostart_disable_noop_success"):
            # Reports success while the unit stays enabled. The caller must
            # trust an independent unit-file-state probe, never this code.
            return 0
        entry["service_enabled"] = False
        _save_state(state)
        return 0
    if args == ["enable", "hubinet-ops"]:
        call_number = state.get("service_autostart_enable_calls", 0) + 1
        state["service_autostart_enable_calls"] = call_number
        _save_state(state)
        if _fail("service_autostart_enable"):
            return 1
        if _fail("service_autostart_enable_noop_success"):
            return 0
        entry["service_enabled"] = True
        _save_state(state)
        return 0
    if args == ["stop", "hubinet-ops"]:
        call_number = state.get("service_stop_calls", 0) + 1
        state["service_stop_calls"] = call_number
        _save_state(state)
        fail_nth = SCENARIO.get("fail_nth_service_stop")
        if fail_nth is not None and call_number == int(fail_nth):
            return 1
        if _fail("service_stop_mutate_then_fail") and call_number == 1:
            entry["service"] = "inactive"
            _save_state(state)
            return 1
        if _fail("service_stop"):
            return 1
        entry["service"] = "inactive"
        _save_state(state)
        return 0
    if args == ["start", "hubinet-ops"]:
        call_number = state.get("service_start_calls", 0) + 1
        state["service_start_calls"] = call_number
        _save_state(state)
        if _fail("service_start_mutate_then_fail") and call_number == 1:
            entry["service"] = "active"
            _save_state(state)
            return 1
        if _fail("service_start_after_stop"):
            return 1
        entry["service"] = "active"
        _save_state(state)
        if SCENARIO.get("kill_updater_after_target_start") and call_number == 1:
            # Later interruption witness: target service has actually
            # started, but acceptance/source-marker completion has not.
            os.kill(os.getppid(), signal.SIGKILL)
        kill_after_call = SCENARIO.get("kill_updater_after_service_start_call")
        if kill_after_call is not None and call_number == int(kill_after_call):
            # Same untrappable-disappearance shape, aimed at an arbitrary
            # start in the run. Start #2 is the ROLLBACK's own restart of
            # the restored old installation -- the point at which the old
            # service is live again and may legitimately write new
            # authority state, while the journal is still active.
            os.kill(os.getppid(), signal.SIGKILL)
        return 0
    if args == ["daemon-reload"]:
        call_number = state.get("daemon_reload_calls", 0) + 1
        state["daemon_reload_calls"] = call_number
        _save_state(state)
        fail_nth = SCENARIO.get("fail_nth_daemon_reload")
        if fail_nth is not None and call_number == int(fail_nth):
            return 1
        if _fail("daemon_reload"):
            return 1
        return 0
    if args == ["show", "hubinet-ops", "--property=ActiveState", "--value"]:
        call_number = state.get("service_state_probe_calls", 0) + 1
        state["service_state_probe_calls"] = call_number
        _save_state(state)
        fail_nth = SCENARIO.get("fail_nth_service_state_probe")
        fail_after = SCENARIO.get("fail_service_state_probe_after")
        if (
            _fail("service_state_probe")
            or (fail_nth is not None and call_number == int(fail_nth))
            or (fail_after is not None and call_number > int(fail_after))
        ):
            return 1
        state_val = SCENARIO.get("service_state_override", entry.get("service", "inactive"))
        if _fail("service_state_probe_malformed"):
            state_val = "not-a-systemd-state"
        _log("service-state", state_val)
        sys.stdout.write(state_val + "\n")
        return 0
    if args == ["show", "hubinet-ops", "--property=UnitFileState", "--value"]:
        call_number = state.get("service_enabled_probe_calls", 0) + 1
        state["service_enabled_probe_calls"] = call_number
        _save_state(state)
        fail_nth = SCENARIO.get("fail_nth_service_enabled_probe")
        if _fail("service_enabled_probe") or (
            fail_nth is not None and call_number == int(fail_nth)
        ):
            return 1
        unit_file_state = "enabled" if entry.get("service_enabled", False) else "disabled"
        if _fail("service_enabled_probe_malformed"):
            unit_file_state = "not-a-unit-file-state"
        _log("unit-file-state", unit_file_state)
        sys.stdout.write(unit_file_state + "\n")
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
    if args == ["is-active", "nftables"]:
        active = entry.get("nftables_active", False)
        sys.stdout.write(("active" if active else "inactive") + "\n")
        return 0 if active else 3
    if args[:1] == ["enable"] and "nftables" in args:
        return 0
    if args[:1] == ["restart"] and "nftables" in args:
        if _fail("nft_activate"):
            return 1
        # Marks the ruleset most recently `pct push`ed to
        # /etc/nftables.conf as the ACTIVE one for this vmid -- pushing
        # the file alone does not reload a running ruleset, matching real
        # nftables.service semantics; see
        # _firewall_permits_local_and_reply_traffic, which only trusts
        # the pushed file once this flag is set.
        entry["nftables_active"] = True
        _save_state(state)
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


_CIDR_PATTERN = re.compile(r'\b(\d{1,3}(?:\.\d{1,3}){3})/(\d{1,2})\b')


def _canonicalize_cidr_match(match):
    addr, prefix = match.group(1), match.group(2)
    try:
        net = ipaddress.ip_network(f"{addr}/{prefix}", strict=False)
    except ValueError:
        return match.group(0)
    if net.prefixlen == 32:
        return str(net.network_address)
    return str(net)


def _canonicalize_active_nft_text(text):
    """Simulates real nftables' load-time canonicalization of the ACTIVE
    ruleset text `nft list ruleset` reports back -- confirmed against a
    real dogfood run on Proxmox VE 9.2.3 / nftables 1.1.3 (see
    deploy/lib/bootstrap-firewall.sh's _nft_canonical_ha_source_expr and
    _hubinetops_uid for the production-side counterpart). Independent of
    whatever the pushed ruleset FILE actually contains -- this bootstrap's
    own generation already writes the canonical HA-source form directly,
    so this is a no-op for that specific rule in real use, but simulating
    the transform independently (via the real ipaddress module, the same
    semantics the production helper uses) is a more honest, robust fake
    than merely mirroring whatever production happens to already emit.
    Two transforms only, matching the exact two canonicalization classes
    a real host proved -- never a full nftables emulator:
      - any IPv4 CIDR address expression is rewritten to its canonical
        form (a /32 host address displayed bare, any other prefix
        displayed as the network address for that prefix);
      - `meta skuid "hubinetops"` is displayed as the simulated numeric
        UID (SCENARIO["nft_reported_skuid"] if explicitly set -- letting
        a test simulate a real-world mismatch against what `id -u
        hubinetops` itself reports -- otherwise SCENARIO["hubinetops_uid"]).
    """
    reported_uid = SCENARIO.get("nft_reported_skuid")
    if not reported_uid:
        reported_uid = SCENARIO.get("hubinetops_uid", FAKE_HUBINETOPS_UID_DEFAULT)
    text = _CIDR_PATTERN.sub(_canonicalize_cidr_match, text)
    text = text.replace('meta skuid "hubinetops"', f"meta skuid {reported_uid}")
    return text


def _exec_nft(vmid, args):
    if args[:2] == ["-c", "-f"]:
        if _fail("nft_syntax"):
            return 1
        return 0
    if args == ["list", "ruleset"]:
        path = _ct_path(vmid, "/etc/nftables.conf")
        if path.exists():
            sys.stdout.write(_canonicalize_active_nft_text(path.read_text()))
        return 0
    return 2


# ---------------------------------------------------------------------------
# Bounded firewall-semantics invariant (fifth-pass corrective fix, P2-1
# whole-feature review). NOT a full nftables emulator -- this fake never
# tracked real connection state or actually filtered any of its own
# simulated traffic, which is exactly why the original bug (self-generated
# firewall silently blocked the bootstrap's own required loopback/reply
# traffic) went undetected: _exec_curl and _exec_discovery_accept both
# succeeded unconditionally regardless of what ruleset had actually been
# pushed and activated. This closes that blind spot narrowly: a
# structural, order-aware check of the exact ACTIVE ruleset text (the same
# chain-line shape bootstrap-firewall.sh's own
# _verify_chain_rules_exact parses) for the two specific properties the
# bootstrap's later phases actually depend on -- never a real packet/
# conntrack simulation.
# ---------------------------------------------------------------------------

def _chain_lines(ruleset_text, chain_name):
    """Same non-boilerplate-line extraction as bootstrap-firewall.sh's own
    _verify_chain_rules_exact (awk version) -- reimplemented here in
    python so the fake's own semantic check parses the exact same shape
    the production verifier does, independently of it.
    """
    marker = f"chain {chain_name} {{"
    in_chain = False
    lines = []
    for raw in ruleset_text.splitlines():
        if marker in raw:
            in_chain = True
            continue
        if not in_chain:
            continue
        stripped = raw.strip()
        if stripped.startswith("}"):
            in_chain = False
            continue
        if stripped.startswith("type filter hook") or stripped == "":
            continue
        lines.append(stripped)
    return lines


def _input_grants_loopback(input_lines):
    target = 'iifname "lo" accept'
    if target not in input_lines:
        return False
    accept_idx = input_lines.index(target)
    # A "tcp dport 8787 drop"-shaped fallthrough appearing BEFORE the
    # loopback accept would already have discarded the packet -- order
    # matters, not merely presence.
    return not any(
        line == "tcp dport 8787 drop" and i < accept_idx
        for i, line in enumerate(input_lines)
    )


def _output_grants_established_replies(output_lines):
    target = "ct state established,related accept"
    if target not in output_lines:
        return False
    accept_idx = output_lines.index(target)
    return not any(
        line == 'meta skuid "hubinetops" drop' and i < accept_idx
        for i, line in enumerate(output_lines)
    )


def _firewall_permits_local_and_reply_traffic(vmid, state):
    """True only if THIS vmid's firewall has actually been activated
    (`systemctl restart nftables` succeeded -- pushing the file alone does
    not reload the running ruleset, matching real semantics) AND the
    active ruleset text grants both invariants Phase 12's own network
    calls (and any real HA client's reply traffic) structurally require:
    loopback ingress before the 8787 fallthrough drop, and established/
    related reply egress before the final hubinetops drop.
    """
    entry = state["vmids"].get(str(vmid), {})
    if not entry.get("nftables_active"):
        return False
    path = _ct_path(vmid, "/etc/nftables.conf")
    if not path.exists():
        return False
    text = path.read_text()
    return _input_grants_loopback(_chain_lines(text, "input")) and _output_grants_established_replies(
        _chain_lines(text, "output")
    )


def _exec_curl(vmid, args):
    # Only the unauthenticated liveness probe (GET /r0/v1/health) is ever
    # curl'd by the bootstrap script now -- the authenticated snapshot/
    # backend checks run entirely inside the simulated
    # hubinet-ops-bootstrap-accept.py invocation above, never via curl,
    # so no bearer token ever reaches this function's argv.
    url = args[-1]
    if "/r0/v1/health" in url:
        if _fail("backend_health"):
            return 7
        state = _load_state()
        # "health_fail_first_n": N -- the first N health requests of this
        # run get no answer, every later one succeeds. Deterministic
        # (a persisted call counter, never a wall-clock sleep) simulation
        # of the ordinary Type=simple readiness race: systemd reports the
        # unit active as soon as the process is exec'd, which is strictly
        # earlier than the moment uvicorn has bound 127.0.0.1:8787.
        fail_first = SCENARIO.get("health_fail_first_n")
        if fail_first is not None:
            call_number = state.get("backend_health_calls", 0) + 1
            state["backend_health_calls"] = call_number
            _save_state(state)
            if call_number <= int(fail_first):
                return 7
        if not _firewall_permits_local_and_reply_traffic(vmid, state):
            # Simulates the packet being silently dropped by this CT's own
            # active firewall -- curl reports "no response" (exit 7),
            # matching the real symptom of a connection that never
            # receives a reply.
            return 7
        sys.stdout.write(SCENARIO.get("health_body", '{"status": "ok"}'))
        return 0
    return 7


def _ambiguous_mode(op_key):
    """None if this op isn't configured for the ownership-race fake path;
    otherwise "self" (mutate using the REAL argv comment this call
    received, simulating THIS run's own call silently succeeding server-
    side despite reporting failure) or a literal replacement comment
    string (simulating a DIFFERENT/foreign creator -- e.g. a concurrent
    bootstrap run that won a race for the same fixed name -- having
    already created the object under a comment that does NOT carry this
    run's own BOOTSTRAP_RUN_ID). Either way, the op still returns exit 1,
    matching the real "ambiguous mutate-then-error" PVE behavior this
    fake exists to simulate. See default_scenario()'s docstring-level
    comment and tests/test_bootstrap_proxmox_0_5_smoke.py's
    TestPveIdentityOwnership for how tests use this.
    """
    return SCENARIO.get("ambiguous_pveum_ops", {}).get(op_key)


def _output_override(key):
    """Raw JSON text to emit verbatim instead of the normally computed
    output for one PVE listing/read-back call -- fourth-pass corrective
    addition, used to simulate a syntactically VALID but schema-
    unexpected PVE response, e.g. [{}], ["hubinetops@pve"],
    [{"user": "..."}] instead of {"userid": ...}, or a JSON array where an
    object is expected (token permissions). This is a distinct failure
    class from _fail() (command failure) and "malformed_pveum_output"
    (genuinely invalid JSON) -- checked after both of those so all three
    stay independently testable. Keyed the same as "malformed_pveum_output":
    "user_list" / "role_list" / "token_list" / "token_permissions".
    """
    return SCENARIO.get("pveum_output_override", {}).get(key)


def cmd_pveum(args):
    _log("pveum", *args)
    state = _load_state()

    if args[:2] == ["user", "list"]:
        # Fail-open regression coverage (ADDITIONAL P2, third pass):
        # "pveum_user_list" simulates the listing command itself failing
        # on EVERY call (transient pveum error, unreachable pmxcfs, etc.);
        # the "user_list" key of "malformed_pveum_output" simulates a
        # successful call whose output is not valid JSON on every call.
        # Both must be a hard stop at phase6's pre-existing-conflict
        # check, never silently read as "absent."
        #
        # The call-count thresholds below (*_after_calls) instead let a
        # single call number onward fail/malform -- needed to test P2-3's
        # rollback ownership live-read-back specifically: phase6's own
        # pre-existing-conflict check is always call #1 and must succeed
        # for the run to ever create the user at all, but
        # _user_object_owned_by_this_run's later rollback read-back is
        # call #2 (no other code path calls `pveum user list`) -- a
        # threshold of 1 fails/malforms starting exactly there.
        state["pveum_user_list_calls"] = state.get("pveum_user_list_calls", 0) + 1
        call_num = state["pveum_user_list_calls"]
        _save_state(state)
        fail_after = SCENARIO.get("pveum_user_list_fail_after_calls")
        malformed_after = SCENARIO.get("pveum_user_list_malformed_after_calls")
        # Seventh-pass corrective addition: the "_after_calls" thresholds
        # above apply to every call from that number ONWARD, which cannot
        # express "call #2 is malformed but call #3 (a later, distinct
        # diagnostic-only re-read) is normal again" -- exactly the real
        # dogfood #2 shape (a schema-invalid rollback read followed
        # immediately by a schema-valid manual read). These "_at_call"
        # variants target exactly one call number and leave every other
        # call's behavior untouched.
        fail_at = SCENARIO.get("pveum_user_list_fail_at_call")
        malformed_at = SCENARIO.get("pveum_user_list_malformed_at_call")
        if (
            _fail("pveum_user_list")
            or (fail_after is not None and call_num > fail_after)
            or (fail_at is not None and call_num == fail_at)
        ):
            return 1
        if (
            SCENARIO.get("malformed_pveum_output", {}).get("user_list")
            or (malformed_after is not None and call_num > malformed_after)
            or (malformed_at is not None and call_num == malformed_at)
        ):
            sys.stdout.write("not-valid-json{{{\n")
            return 0
        # Same call-count-gating reasoning as fail_after/malformed_after
        # above -- an override with no threshold given applies to every
        # call (including phase6's own pre-check); "override_after_calls"
        # lets a test target the rollback read-back (call #2) specifically
        # while leaving phase6's own first call unaffected.
        override_after = SCENARIO.get("pveum_user_list_override_after_calls")
        override = _output_override("user_list")
        if override is not None and (override_after is None or call_num > override_after):
            sys.stdout.write(override)
            return 0
        print(json.dumps([
            {"userid": u, "comment": info.get("comment", "")}
            for u, info in state["pve_users"].items()
        ]))
        return 0
    if args[:2] == ["role", "list"]:
        if _fail("pveum_role_list"):
            return 1
        if SCENARIO.get("malformed_pveum_output", {}).get("role_list"):
            sys.stdout.write("not-valid-json{{{\n")
            return 0
        override = _output_override("role_list")
        if override is not None:
            sys.stdout.write(override)
            return 0
        print(json.dumps([{"roleid": r} for r in state["pve_roles"].keys()]))
        return 0
    if args[:2] == ["user", "add"]:
        comment = args[args.index("--comment") + 1] if "--comment" in args else ""
        ambiguous = _ambiguous_mode("user_add")
        if ambiguous is not None:
            stored_comment = comment if ambiguous == "self" else ambiguous
            state["pve_users"][args[2]] = {"comment": stored_comment}
            _save_state(state)
            return 1
        if _fail("pveum_user_add"):
            return 1
        state["pve_users"][args[2]] = {"comment": comment}
        _save_state(state)
        return 0
    if args[:2] == ["role", "add"]:
        rolename = args[2]
        privs_csv = args[args.index("--privs") + 1] if "--privs" in args else ""
        privs = [p for p in privs_csv.split(",") if p]
        # role_privs_override simulates a real-world PVE-side discrepancy
        # between the privileges requested and the privileges the role
        # actually ends up with -- the mechanism this fake uses to
        # exercise the verification logic's negative paths (missing
        # required / extra mutation-shaped privilege) without ever
        # weakening the bootstrap script's own fixed, correct request.
        overrides = SCENARIO.get("role_privs_override", {})
        if rolename in overrides:
            privs = overrides[rolename]
        ambiguous = _ambiguous_mode("role_add")
        if ambiguous is not None:
            # PVE roles have no comment field -- "self" and "foreign" are
            # indistinguishable for this object type; either way the
            # mutation still happens (an object now exists) but is
            # reported as a failure, and ownership can only ever be
            # decided via this run's own ledger (never proven here).
            state["pve_roles"][rolename] = privs
            _save_state(state)
            return 1
        if _fail("pveum_role_add"):
            return 1
        state["pve_roles"][rolename] = privs
        _save_state(state)
        return 0
    if args[:2] == ["acl", "modify"]:
        if _fail("pveum_acl_modify"):
            return 1
        rolename = args[args.index("--roles") + 1] if "--roles" in args else None
        if "--users" in args and rolename:
            target = f"user:{args[args.index('--users') + 1]}"
            state["acl_grants"].append({"target": target, "role": rolename})
        if "--tokens" in args and rolename:
            target = f"token:{args[args.index('--tokens') + 1]}"
            state["acl_grants"].append({"target": target, "role": rolename})
        _save_state(state)
        return 0
    if args[:2] == ["acl", "delete"]:
        rolename = args[args.index("--roles") + 1] if "--roles" in args else None
        if "--users" in args:
            target = f"user:{args[args.index('--users') + 1]}"
        elif "--tokens" in args:
            target = f"token:{args[args.index('--tokens') + 1]}"
        else:
            target = None
        if target:
            state["acl_grants"] = [
                g for g in state["acl_grants"]
                if not (g["target"] == target and g["role"] == rolename)
            ]
            _save_state(state)
        return 0
    if args[:3] == ["user", "token", "add"]:
        # args shape: user token add <user> <tokenid> --privsep 1 ...
        user, token_id = args[3], args[4]
        comment = args[args.index("--comment") + 1] if "--comment" in args else ""
        full = f"{user}!{token_id}"
        ambiguous = _ambiguous_mode("token_add")
        if ambiguous is not None:
            stored_comment = comment if ambiguous == "self" else ambiguous
            state["pve_tokens"][full] = {"comment": stored_comment}
            _save_state(state)
            return 1
        if _fail("pveum_token_add"):
            return 1
        state["pve_tokens"][full] = {"comment": comment}
        _save_state(state)
        print(json.dumps({
            "full-tokenid": full,
            "info": {"privsep": "1"},
            "value": SCENARIO.get("pve_token_secret", "00000000-0000-0000-0000-000000000000"),
        }))
        return 0
    if args[:3] == ["user", "token", "list"]:
        # args shape: user token list <user> --output-format json. Unlike
        # "user list" above, this used to be called ONLY from
        # _token_object_owned_by_this_run's rollback read-back -- no
        # earlier successful-path code calls it. Seventh-pass corrective
        # addition: the diagnostic-only re-read (bootstrap-common.sh's
        # _diagnostic_ownership_reread) can now call it a SECOND time in
        # the same rollback, so a call counter and "_at_call" hooks are
        # needed here too, mirroring "user list" above -- to let a test
        # target the first (authoritative) read and the second
        # (diagnostic-only) read independently.
        state["pveum_token_list_calls"] = state.get("pveum_token_list_calls", 0) + 1
        call_num = state["pveum_token_list_calls"]
        _save_state(state)
        fail_at = SCENARIO.get("pveum_token_list_fail_at_call")
        malformed_at = SCENARIO.get("pveum_token_list_malformed_at_call")
        if _fail("pveum_token_list") or (fail_at is not None and call_num == fail_at):
            return 1
        if SCENARIO.get("malformed_pveum_output", {}).get("token_list") or (
            malformed_at is not None and call_num == malformed_at
        ):
            sys.stdout.write("not-valid-json{{{\n")
            return 0
        override = _output_override("token_list")
        if override is not None:
            sys.stdout.write(override)
            return 0
        user = args[3]
        prefix = f"{user}!"
        entries = [
            {"tokenid": full[len(prefix):], "comment": info.get("comment", "")}
            for full, info in state["pve_tokens"].items()
            if full.startswith(prefix)
        ]
        print(json.dumps(entries))
        return 0
    if args[:3] == ["user", "token", "permissions"]:
        # args shape: user token permissions <user> <tokenid> --path <path> ...
        #
        # Real-PVE corrective note: this used to emit a flat object of
        # privilege names directly at the top level ({"Sys.Audit": 1,
        # ...}). A real-host read-only precheck against Proxmox VE 9.2.3
        # disproved that assumption -- the real command returns a
        # path-keyed object instead ({"<path>": {"Sys.Audit": 1, ...}}),
        # observed literally as `{"/":{}}` for an empty grant at path "/".
        # See the real-PVE
        # precheck notes. The requested --path value is read back from
        # argv (defaulting to "/", the only value this bootstrap ever
        # requests) rather than hardcoded, so a test could in principle
        # exercise a different path too.
        if _fail("pveum_token_permissions"):
            return 1
        override = _output_override("token_permissions")
        if override is not None:
            sys.stdout.write(override)
            return 0
        requested_path = args[args.index("--path") + 1] if "--path" in args else "/"
        full_token_id = f"{args[3]}!{args[4]}"
        privs = set()
        for grant in state["acl_grants"]:
            if grant["target"] == f"token:{full_token_id}":
                privs.update(state["pve_roles"].get(grant["role"], []))
        print(json.dumps({requested_path: {p: 1 for p in sorted(privs)}}))
        return 0
    if args[:3] == ["user", "token", "remove"]:
        # args shape: user token remove <user> <tokenid>
        # Ninth-pass corrective addition (P2 finding, independent
        # review): rollback_on_failure's parent_user_cleanup_safe gate
        # requires this command's OWN exit status, not merely "the
        # token's ownership was proven" -- "pveum_user_token_remove" in
        # the "fail" list simulates the removal command itself failing
        # (e.g. a transient pveum error) even though the token was
        # correctly proven owned.
        if _fail("pveum_user_token_remove"):
            return 1
        full = f"{args[3]}!{args[4]}"
        state["pve_tokens"].pop(full, None)
        _save_state(state)
        return 0
    if args[:2] == ["role", "delete"]:
        rolename = args[2]
        state["pve_roles"].pop(rolename, None)
        _save_state(state)
        return 0
    if args[:2] == ["user", "delete"]:
        # Tenth-pass corrective addition (P3 finding, independent review):
        # a successful deletion of the owning user also removes every
        # token registered under it (tokenid prefix "<user>!") -- this is
        # the exact real Proxmox hazard rollback_on_failure's
        # parent_user_cleanup_safe gate exists to prevent for an unproven
        # token. An earlier version of this handler only removed the user
        # entry, leaving that user's tokens in state["pve_tokens"]
        # untouched regardless of whether the delete call was safe or
        # not -- materially less faithful than real Proxmox for the exact
        # hazard this repository's rollback logic is built around, so a
        # test asserting a token survived could pass even if the
        # (unsafe) `pveum user delete` call had actually been made.
        user = args[2]
        if user in state["pve_users"]:
            state["pve_users"].pop(user, None)
            prefix = f"{user}!"
            for full_token_id in list(state["pve_tokens"].keys()):
                if full_token_id.startswith(prefix):
                    state["pve_tokens"].pop(full_token_id, None)
            _save_state(state)
        return 0
    _log("pveum", "UNHANDLED", *args)
    return 2


def cmd_pveam(args):
    _log("pveam", *args)
    if args[:1] == ["update"]:
        if _fail("pveam_update"):
            return 1
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


def cmd_dpkg(args):
    _log("dpkg", *args)
    if args == ["--print-architecture"]:
        if _fail("dpkg_arch"):
            return 1
        print(SCENARIO.get("host_debian_arch", "amd64"))
        return 0
    return 2


def cmd_pvesh(args):
    _log("pvesh", *args)
    state = _load_state()
    # Windows Git-Bash only: MSYS can mangle a POSIX-looking absolute-path
    # argument like "/cluster/nextid" into a real Windows path (e.g.
    # "C:/Program Files/Git/cluster/nextid") before this dispatcher (a
    # native python.exe) ever sees it -- matching by suffix instead of
    # exact equality survives that no-op-on-Linux mangling. See
    # _normalize_ct_arg above for the same class of issue elsewhere in
    # this file.
    if (
        args[:1] == ["get"]
        and len(args) > 1
        and args[1].replace("\\", "/").endswith("/cluster/nextid")
    ):
        candidates = SCENARIO.get("next_free_vmid", FAKE_NEXT_VMID)
        if isinstance(candidates, list):
            idx = state.get("nextid_call_count", 0)
            value = candidates[min(idx, len(candidates) - 1)]
            state["nextid_call_count"] = idx + 1
            _save_state(state)
        else:
            value = candidates
        print(json.dumps(value))
        return 0
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
            # Real `pvesm status` reports Total/Used/Available in KiB, not
            # bytes -- this fake's default and every scenario override
            # must stay KiB-scaled to actually exercise the production
            # parser's real unit assumption (a prior version of this fake
            # used bytes, which never would have caught the KiB unit bug
            # the production code had).
            avail_kib = SCENARIO.get("storage_available_kib", 100 * 1024 * 1024)
            avail_field = str(avail_kib) if avail_kib is not None else SCENARIO.get("storage_available_raw", "not-a-number")
            total_kib = 200 * 1024 * 1024
            used_kib = max(total_kib - (avail_kib or 0), 0)
            print("Name Type Status Total Used Available %")
            print(f"{name} dir active {total_kib} {used_kib} {avail_field} 1.00")
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


def cmd_cmp(args):
    _log("cmp", *args)
    if _fail("cmp_error"):
        return 2
    operands = [arg for arg in args if not arg.startswith("-")]
    if len(operands) != 2:
        return 2
    try:
        left = Path(operands[0]).read_bytes()
        right = Path(operands[1]).read_bytes()
    except OSError:
        return 2
    return 0 if left == right else 1


DISPATCH = {
    "pct": cmd_pct,
    "pveum": cmd_pveum,
    "pveam": cmd_pveam,
    "pvesh": cmd_pvesh,
    "pvesm": cmd_pvesm,
    "dpkg": cmd_dpkg,
    "cmp": cmd_cmp,
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

    def state(self) -> dict[str, Any]:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def simulate_pve_ct_reboot(self, vmid: str) -> None:
        """One bounded PVE-host/CT restart, over the state this fake already
        tracks -- not a second reboot simulator.

        Semantics, exactly the ones the updater's autostart guard depends on:

        - the CT comes back RUNNING when its installation-time onboot flag
          says so (bootstrap's final state is onboot=1, and the updater
          never changes it);
        - inside the CT, systemd boot-activates hubinet-ops IF AND ONLY IF
          the unit file is currently ENABLED. A unit the updater disabled
          for its mutation window stays inactive, so no half-swapped
          app/venv/unit/helper/database state can ever auto-run;
        - the container's /tmp is VOLATILE and comes back empty, exactly
          as a real restarted CT's does (correction pass 7, P1). This
          fake previously preserved it, which silently hid the updater's
          dependence on run-owned /tmp helpers surviving a reboot.

        Nothing else about the installation is invented here. Everything
        durable survives the restart exactly as it was -- /opt rollback
        and staged artifacts, /var/lib authority backups and data, and the
        host-side updater journal, lock and helper artifacts -- which is
        the whole point.
        """
        ct_tmp = self.ct_root / vmid / "tmp"
        if ct_tmp.exists():
            shutil.rmtree(ct_tmp)
        ct_tmp.mkdir(parents=True)

        state = self.state()
        entry = state["vmids"].setdefault(vmid, {})
        entry["started"] = str(entry.get("onboot", "1")) == "1"
        if entry["started"] and entry.get("service_enabled", False):
            entry["service"] = "active"
        else:
            entry["service"] = "inactive"
        self.state_path.write_text(json.dumps(state), encoding="utf-8")


def default_scenario() -> dict[str, Any]:
    return {
        "fail": [],
        "ct_ip": FAKE_CT_IP,
        "next_free_vmid": FAKE_NEXT_VMID,
        "local_templates": [FAKE_TEMPLATE_VOLID],
        "available_templates": [f"system {FAKE_TEMPLATE_FILENAME}"],
        "host_debian_arch": "amd64",
        "bridges": [FAKE_BRIDGE],
        "storages": {"rootdir": [FAKE_STORAGE], "vztmpl": [FAKE_TEMPLATE_STORAGE]},
        # KiB, matching real `pvesm status` output -- 100 GiB.
        "storage_available_kib": 100 * 1024 * 1024,
        "role_privs_override": {},
        "pve_token_secret": FAKE_PVE_TOKEN_SECRET,
        "r0_api_token": FAKE_R0_API_TOKEN,
        "systemd_status": "running",
        "failed_units": [],
        "legacy_present": {},
        "health_body": '{"status": "ok"}',
        "ct_tools_available": ["nft", "curl", "ss", "ssh", "ssh-keygen"],
        "discovery_result": "healthy",
        "discovery_backend_instance_id": "fake-backend-instance-id",
        "discovery_source_name": FAKE_DISPLAY_NAME,
        "discovery_resource_count": 0,
        # Third-pass corrective additions -- see cmd_pveum/cmd_pct and
        # _maybe_replace_identity_before_failure for how each is consumed.
        # Left at their "no effect" defaults here; individual tests
        # override only the key(s) they need.
        "malformed_pveum_output": {},
        "pveum_output_override": {},
        "replace_identity_before_failure": {},
        "pveum_user_list_fail_after_calls": None,
        "pveum_user_list_malformed_after_calls": None,
        "pveum_user_list_override_after_calls": None,
        # Seventh-pass corrective additions -- see the "_at_call" handling
        # notes at each cmd_pveum call site above.
        "pveum_user_list_fail_at_call": None,
        "pveum_user_list_malformed_at_call": None,
        "pveum_token_list_fail_at_call": None,
        "pveum_token_list_malformed_at_call": None,
        "ct_actual_resolv_conf": None,
        # Sixth-pass corrective additions (real-PVE nft canonicalization)
        # -- see _exec_inner's "id -u hubinetops" handler and
        # _canonicalize_active_nft_text.
        "hubinetops_uid": FAKE_HUBINETOPS_UID,
        "hubinetops_uid_malformed": False,
        "nft_reported_skuid": None,
    }


_FAKE_COMMANDS = ("pct", "pveum", "pveam", "pvesh", "pvesm", "nft", "dpkg", "cmp")


def build_minimal_source_checkout(tmp_path: Path, repo_root: Path) -> Path:
    """A tiny, fast, git-initialized stand-in for --source-dir.

    Tests must not depend on the real developer working tree's incidental
    contents (leftover local `.pytest-tmp-*`/`.tmp-pytest-*` scratch
    directories are common and can be very large) or on its own git
    history. This copies only the exact files phase 8 actually reads
    (deploy/install-0.5.0-fresh.sh itself is intentionally NOT copied
    here for most tests -- the fake `pct exec ... bash .../install-0.5.0-
    fresh.sh` step is scenario-driven and never executes real content),
    then `git init`s and commits it so `git archive <sha>` behaves exactly
    like it would against a real release checkout. `git` itself is the
    real system binary (see this module's docstring) -- Mandatory Fix 4/6
    is specifically about real git provenance behavior (clean worktree,
    exact full SHA, `git archive <sha>` not `HEAD`), so faking it would
    defeat the point of testing it.
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
    (src / "deploy" / "hubinet-package-scan-helper.py").write_text(
        (repo_root / "deploy" / "hubinet-package-scan-helper.py").read_text(
            encoding="utf-8"
        ),
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


def git_head_sha(source_dir: Path) -> str:
    import subprocess as _subprocess

    result = _subprocess.run(
        ["git", "-C", str(source_dir), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def build_fake_pve_environment(tmp_path: Path, scenario: dict[str, Any] | None = None) -> FakePveEnvironment:
    scenario = scenario if scenario is not None else default_scenario()

    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    ct_root = tmp_path / "ctroot"
    ct_root.mkdir(exist_ok=True)
    host_root = tmp_path / "hostroot"
    (host_root / "etc" / "ssh").mkdir(parents=True, exist_ok=True)
    (host_root / "etc" / "ssh" / "ssh_host_ed25519_key.pub").write_text(
        "ssh-ed25519 QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE= fake-pve\n",
        encoding="utf-8",
    )
    log_path = tmp_path / "fake-command.log"
    scenario_path = tmp_path / "scenario.json"
    state_path = tmp_path / "state.json"
    dispatcher_path = tmp_path / "_dispatcher.py"

    scenario_path.write_text(json.dumps(scenario))
    state_path.write_text(json.dumps({
        "vmids": {}, "pve_users": {}, "pve_roles": {}, "pve_tokens": {},
        "acl_grants": [], "nextid_call_count": 0,
    }))
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
    # AGENTS.md: "use an isolated PATH without inheriting the host PATH,
    # ... and add only an explicit allowlist of unprivileged local tools."
    # On the real (Linux, Docker-based) sandbox this smoke suite is
    # restricted to, `bin_dir` plus the standard system directories is a
    # genuine explicit allowlist -- it excludes anything a CI runner's own
    # ambient PATH might otherwise contribute. sys.executable's own
    # directory is included so the dispatcher shims (which exec
    # sys.executable by absolute path already, independent of PATH) still
    # have a working `python3` if any fake ever needs to invoke it by
    # bare name. Windows developer machines (where this module is also
    # exercised locally, never as the compliance-relevant run) keep
    # inheriting the full host PATH instead, since MSYS/Git-for-Windows
    # tool locations vary and this platform is never the real sandbox
    # AGENTS.md's rule targets (Linux + Docker only).
    if sys.platform.startswith("linux"):
        allowlisted_dirs = [str(bin_dir), "/usr/local/bin", "/usr/bin", "/bin", str(Path(python_exe).parent)]
        env["PATH"] = os.pathsep.join(dict.fromkeys(allowlisted_dirs))
    else:
        env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["HUBINET_FAKE_LOG"] = str(log_path)
    env["HUBINET_FAKE_SCENARIO"] = str(scenario_path)
    env["HUBINET_FAKE_CT_ROOT"] = str(ct_root)
    env["HUBINET_FAKE_STATE"] = str(state_path)
    env["HUBINET_OPS_TEST_MODE"] = "1"
    env["HUBINET_OPS_TEST_HOST_ROOT"] = str(host_root)
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
    env.setdefault("BOOTSTRAP_DISCOVERY_TIMEOUT_SECONDS", "2")

    return FakePveEnvironment(
        bin_dir=bin_dir,
        ct_root=ct_root,
        log_path=log_path,
        scenario_path=scenario_path,
        state_path=state_path,
        env=env,
    )
