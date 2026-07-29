#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from hubinet_ops_release import FINGERPRINT_RE, inspect_staged_release, public_release

SNAPSHOT_RE = re.compile(
    r"^hubinet-ops-(?P<vmid>[1-9][0-9]{1,5})-"
    r"(?P<kind>pre-update|pre|manual|man)-(?P<stamp>[0-9]{8}T[0-9]{6}Z)$"
)
SOURCE_JOB_RE = re.compile(r"^[a-f0-9]{8,64}$")
RESOURCE_TYPES = {"lxc", "qemu"}
READ_ONLY_ACTIONS = {
    "status",
    "inspect",
    "capabilities",
    "list-snapshots",
    "self-update-release",
}
LIFECYCLE_ACTIONS = {"start", "shutdown", "reboot", "force-stop"}
MANAGED_ACTIONS = {
    "scan": "check-updates",
    "preflight": "preflight",
    "update": "update",
    "healthcheck": "healthcheck",
    "repair": "repair",
    "verify": "verify",
}
SNAPSHOT_ACTIONS = {"snapshot-create", "snapshot-rollback", "snapshot-delete"}
ALIASES = {
    "snapshot": "snapshot-create",
    "rollback": "snapshot-rollback",
    "delete-snapshot": "snapshot-delete",
}
ALLOWED_ACTIONS = (
    READ_ONLY_ACTIONS
    | LIFECYCLE_ACTIONS
    | set(MANAGED_ACTIONS)
    | SNAPSHOT_ACTIONS
    | set(ALIASES)
    | {"self-update"}
)


class HostControlError(RuntimeError):
    pass


@dataclass(frozen=True)
class HostPaths:
    observation: Path = Path("/etc/hubinet-ops/observation-vmids")
    managed: Path = Path("/etc/hubinet-ops/managed-vmids")
    maintenance: Path = Path("/etc/hubinet-ops/maintenance-vmids")
    lifecycle: Path = Path("/etc/hubinet-ops/lifecycle-vmids")
    host_control: Path = Path("/etc/hubinet-ops/host-control-vmids")
    snapshot_create: Path = Path("/etc/hubinet-ops/snapshot-create-vmids")
    snapshot_restore: Path = Path("/etc/hubinet-ops/snapshot-restore-vmids")
    snapshot_delete: Path = Path("/etc/hubinet-ops/snapshot-delete-vmids")
    resource_types: Path = Path("/etc/hubinet-ops/resource-types")
    pve_local: Path = Path("/etc/pve/local")
    pve_nodes: Path = Path("/etc/pve/nodes")
    self_update: Path = Path("/usr/local/sbin/hubinet-ops-self-update")
    self_update_release: Path = Path("/var/lib/hubinet-ops-hostd/approved-release")


class HostPolicy:
    def __init__(self, paths: HostPaths = HostPaths()) -> None:
        self.paths = paths
        self.observation = _read_vmids(paths.observation)
        self.managed = _read_vmids(paths.managed)
        self.maintenance = _read_vmids(paths.maintenance)
        self.lifecycle = _read_vmids(paths.lifecycle)
        self.host_control = _read_vmids(paths.host_control)
        self.snapshot_create = _read_vmids(paths.snapshot_create)
        self.snapshot_restore = _read_vmids(paths.snapshot_restore)
        self.snapshot_delete = _read_vmids(paths.snapshot_delete)
        self.resource_types = _read_resource_types(paths.resource_types)

    def validate(
        self,
        action: str,
        vmid: int,
        argument: str | None = None,
    ) -> tuple[str, int, str | None, str]:
        normalized = ALIASES.get(str(action), str(action))
        if normalized not in ALLOWED_ACTIONS - set(ALIASES):
            raise HostControlError("Action not allowed")
        if isinstance(vmid, bool) or not 1 <= int(vmid) <= 999999:
            raise HostControlError("Invalid VMID")
        vmid = int(vmid)
        resource_type = self.resource_types.get(vmid)
        if resource_type not in RESOURCE_TYPES or vmid not in self.observation:
            raise HostControlError("VMID not observation allowed")
        if normalized in set(MANAGED_ACTIONS) | {"capabilities"}:
            if resource_type != "lxc" or vmid not in self.managed:
                raise HostControlError("VMID not managed-executor allowed")
        if normalized in {"update", "repair"} and vmid not in self.maintenance:
            raise HostControlError("VMID not maintenance allowed")
        if normalized in LIFECYCLE_ACTIONS:
            if resource_type != "lxc" or vmid not in self.lifecycle:
                raise HostControlError("VMID not lifecycle allowed")
        if normalized in SNAPSHOT_ACTIONS:
            if resource_type != "lxc" or vmid not in self.host_control:
                raise HostControlError("VMID not host-control allowed")
            action_policy = {
                "snapshot-create": (self.snapshot_create, "snapshot create"),
                "snapshot-rollback": (self.snapshot_restore, "snapshot restore"),
                "snapshot-delete": (self.snapshot_delete, "snapshot delete"),
            }[normalized]
            if vmid not in action_policy[0]:
                raise HostControlError(
                    f"VMID not {action_policy[1]} allowed by PVE policy"
                )
        if normalized in {"self-update", "self-update-release"} and vmid != 110:
            raise HostControlError("Self-update is allowed only for CT110")
        if normalized in SNAPSHOT_ACTIONS:
            if not argument or not owned_snapshot(argument, vmid):
                raise HostControlError("Snapshot is not owned by Hubinet Ops")
        elif normalized == "self-update":
            if not argument or not FINGERPRINT_RE.fullmatch(argument):
                raise HostControlError("Self-update requires an approved release fingerprint")
        elif argument is not None:
            raise HostControlError("Action does not accept an argument")
        return normalized, vmid, argument, resource_type


Runner = Callable[..., subprocess.CompletedProcess[str]]


class HostController:
    def __init__(
        self,
        policy: HostPolicy | None = None,
        *,
        runner: Runner = subprocess.run,
    ) -> None:
        self.policy = policy or HostPolicy()
        self.runner = runner

    def execute(
        self,
        action: str,
        vmid: int,
        argument: str | None = None,
        *,
        source_job_id: str | None = None,
    ) -> dict[str, Any]:
        action, vmid, argument, resource_type = self.policy.validate(
            action, vmid, argument
        )
        if source_job_id is not None and not SOURCE_JOB_RE.fullmatch(source_job_id):
            raise HostControlError("Invalid source job ID")
        if action == "status":
            return self._status(vmid, resource_type)
        if action == "inspect":
            return self._inspect(vmid, resource_type)
        if action == "self-update-release":
            try:
                return public_release(
                    inspect_staged_release(self.policy.paths.self_update_release)
                )
            except RuntimeError as exc:
                raise HostControlError(str(exc)) from exc
        if action == "capabilities":
            self._require_running(vmid)
            return self._managed(vmid, "capabilities", timeout=60)
        if action in MANAGED_ACTIONS:
            self._require_running(vmid)
            return self._managed(vmid, MANAGED_ACTIONS[action], timeout=4500)
        if action in LIFECYCLE_ACTIONS:
            return self._lifecycle(vmid, action)
        if action == "list-snapshots":
            if resource_type != "lxc" or vmid not in self.policy.host_control:
                raise HostControlError("Snapshot listing is not allowed")
            return {"snapshots": self.list_snapshots(vmid)}
        if action == "snapshot-create":
            return self._snapshot_create(vmid, str(argument), source_job_id)
        if action == "snapshot-rollback":
            snapshot = self._require_owned_existing_snapshot(vmid, str(argument))
            if not snapshot["rollback_eligible"]:
                raise HostControlError("Snapshot is not restore eligible")
            was_running = self._status(vmid, "lxc")["lxc_status"] == "running"
            if was_running:
                self._lifecycle(vmid, "shutdown")
            try:
                self._run(["pct", "rollback", str(vmid), str(argument)], timeout=900)
            except Exception:
                if was_running:
                    try:
                        self._lifecycle(vmid, "start")
                    except Exception:
                        pass
                raise
            if was_running:
                self._lifecycle(vmid, "start")
            return {
                "snapshot": argument,
                "action": "rollback",
                "lxc_status": "running" if was_running else "stopped",
            }
        if action == "snapshot-delete":
            snapshot = self._require_owned_existing_snapshot(vmid, str(argument))
            if not snapshot["delete_eligible"]:
                raise HostControlError("Snapshot is not delete eligible")
            self._run(["pct", "delsnapshot", str(vmid), str(argument)], timeout=300)
            return {"snapshot": argument, "action": "delete"}
        if action == "self-update":
            script = self.policy.paths.self_update
            if not script.is_file():
                raise HostControlError("CT110 self-update supervisor is not installed")
            if source_job_id is None:
                raise HostControlError("Self-update requires a durable source job ID")
            self._run(
                [
                    str(script),
                    "--job-id",
                    source_job_id,
                    "--fingerprint",
                    str(argument),
                ],
                timeout=60,
            )
            return {
                "action": "self-update",
                "vmid": 110,
                "fingerprint": argument,
                "supervisor_started": True,
            }
        raise HostControlError("Action not implemented")

    def managed_passthrough(self, action: str, vmid: int) -> int:
        normalized, vmid, _, resource_type = self.policy.validate(action, vmid)
        if resource_type != "lxc" or normalized not in set(MANAGED_ACTIONS) | {
            "capabilities"
        }:
            raise HostControlError("Managed passthrough is not allowed")
        self._require_running(vmid)
        guest_action = (
            "capabilities" if normalized == "capabilities" else MANAGED_ACTIONS[normalized]
        )
        completed = self.runner(
            ["pct", "exec", str(vmid), "--", "/usr/local/sbin/hubinet-maint", guest_action],
            text=True,
            capture_output=True,
            timeout=4500,
            check=False,
            shell=False,
        )
        sys.stdout.write(completed.stdout)
        if completed.stderr:
            sys.stderr.write(completed.stderr[-8000:])
        return int(completed.returncode or 0)

    def list_snapshots(self, vmid: int) -> list[dict[str, Any]]:
        node = self._resolve_node()
        raw = self._run(
            [
                "pvesh",
                "get",
                f"/nodes/{node}/lxc/{vmid}/snapshot",
                "--output-format",
                "json",
            ],
            timeout=60,
        ).stdout
        try:
            values = json.loads(raw or "[]")
        except json.JSONDecodeError as exc:
            raise HostControlError("PVE returned invalid snapshot JSON") from exc
        snapshots: list[dict[str, Any]] = []
        for item in values if isinstance(values, list) else []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("snapname") or item.get("name") or "")
            parsed = parse_snapshot(name, vmid)
            created_at = _snapshot_created_at(item, parsed)
            owned = parsed is not None
            current = bool(item.get("current")) or name == "current"
            snapshots.append(
                {
                    "name": name[:128],
                    "description": str(item.get("description") or "")[:1000],
                    "created_at": created_at,
                    "kind": parsed["kind"] if parsed else None,
                    "owned_by_hubinet_ops": owned,
                    "rollback_eligible": owned and not current,
                    "delete_eligible": owned and not current,
                    "source_job_id": _description_job_id(item.get("description")),
                }
            )
        return sorted(
            snapshots,
            key=lambda item: item.get("created_at") or "",
            reverse=True,
        )

    def _status(self, vmid: int, resource_type: str) -> dict[str, Any]:
        command = ["pct", "status", str(vmid)] if resource_type == "lxc" else [
            "qm", "status", str(vmid)
        ]
        raw = self._run(command, timeout=30).stdout.strip()
        state = raw.split()[-1] if raw else "unknown"
        key = "lxc_status" if resource_type == "lxc" else "qemu_status"
        return {"resource_type": resource_type, "runtime_status": state, key: state}

    def _inspect(self, vmid: int, resource_type: str) -> dict[str, Any]:
        status = self._status(vmid, resource_type)
        state = str(status["runtime_status"])
        if resource_type == "lxc":
            if state != "running":
                return {
                    **status,
                    "adapter": "agent_self" if vmid == 110 else "apt",
                    "health_status": "offline",
                    "health_score": 0,
                }
            if vmid == 110:
                return {**status, "adapter": "agent_self"}
            data = self._managed(vmid, "inspect", timeout=180)
            return {**data, **status, "adapter": "apt"}
        node = self._resolve_node()
        current = _json_object(
            self._run(
                [
                    "pvesh", "get", f"/nodes/{node}/qemu/{vmid}/status/current",
                    "--output-format", "json",
                ],
                timeout=30,
            ).stdout
        )
        resources = _json_list(
            self._run(
                ["pvesh", "get", "/cluster/resources", "--type", "vm", "--output-format", "json"],
                timeout=30,
                check=False,
            ).stdout
        )
        cpu = _usage_share(next(
            (item.get("cpu") for item in resources if _same_vmid(item.get("vmid"), vmid)),
            None,
        ))
        raw_disk_used = current.get("disk")
        try:
            disk_used = int(raw_disk_used) if raw_disk_used is not None else None
        except (TypeError, ValueError):
            disk_used = None
        # Proxmox reports disk=0 for QEMU guests when it has no filesystem
        # usage source. Zero is therefore unknown here, not a measured 0 B.
        if disk_used is not None and disk_used <= 0:
            disk_used = None
        return {
            **status,
            "adapter": "haos",
            "health_status": "healthy" if state == "running" else "offline",
            "health_score": 100 if state == "running" else 0,
            "name": str(current.get("name") or "")[:255],
            "uptime_seconds": max(0, int(current.get("uptime") or 0)),
            "cpu": {"usage": cpu, "cores": current.get("cpus")},
            "memory": {"used_bytes": current.get("mem"), "total_bytes": current.get("maxmem")},
            "disk": {
                "used_bytes": disk_used,
                "total_bytes": current.get("maxdisk"),
                "usage_known": disk_used is not None,
            },
            "network": {"in_bytes": current.get("netin"), "out_bytes": current.get("netout")},
        }

    def _managed(self, vmid: int, action: str, *, timeout: int) -> dict[str, Any]:
        completed = self._run(
            ["pct", "exec", str(vmid), "--", "/usr/local/sbin/hubinet-maint", action],
            timeout=timeout,
        )
        final: dict[str, Any] | None = None
        for line in completed.stdout.splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and ("ok" in value or value.get("type") == "result"):
                final = value
        if not final or final.get("ok") is not True:
            raise HostControlError(str((final or {}).get("error") or "Invalid managed response"))
        data = final.get("data")
        return dict(data) if isinstance(data, dict) else {}

    def _lifecycle(self, vmid: int, action: str) -> dict[str, Any]:
        current = self._status(vmid, "lxc")["lxc_status"]
        if action == "start" and current != "stopped":
            raise HostControlError(f"start requires stopped runtime, got {current}")
        if action in {"shutdown", "reboot", "force-stop"} and current != "running":
            raise HostControlError(f"{action} requires running runtime, got {current}")
        argv = {
            "start": ["pct", "start", str(vmid)],
            "shutdown": ["pct", "shutdown", str(vmid), "--timeout", "90"],
            "reboot": ["pct", "reboot", str(vmid), "--timeout", "90"],
            "force-stop": ["pct", "stop", str(vmid), "--skiplock", "0"],
        }[action]
        self._run(argv, timeout=180)
        expected = "stopped" if action in {"shutdown", "force-stop"} else "running"
        actual = self._status(vmid, "lxc")["lxc_status"]
        if actual != expected:
            raise HostControlError(f"lifecycle expected {expected}, got {actual}")
        return {"action": action, "lxc_status": actual, "runtime_status": actual}

    def _snapshot_create(
        self,
        vmid: int,
        name: str,
        source_job_id: str | None,
    ) -> dict[str, Any]:
        parsed = parse_snapshot(name, vmid)
        if parsed is None:
            raise HostControlError("Invalid Hubinet Ops snapshot name")
        description = (
            "hubinet-ops;"
            f"kind={parsed['kind']};created_at={_name_created_at(parsed)};"
            f"source_job_id={source_job_id or ''}"
        )
        self._run(
            ["pct", "snapshot", str(vmid), name, "--description", description],
            timeout=600,
        )
        return {
            "name": name,
            "description": description,
            "created_at": _name_created_at(parsed),
            "kind": parsed["kind"],
            "owned_by_hubinet_ops": True,
            "source_job_id": source_job_id,
        }

    def _require_owned_existing_snapshot(self, vmid: int, name: str) -> dict[str, Any]:
        for snapshot in self.list_snapshots(vmid):
            if snapshot["name"] == name:
                if not snapshot["owned_by_hubinet_ops"]:
                    break
                return snapshot
        raise HostControlError("Hubinet Ops snapshot does not exist")

    def _require_running(self, vmid: int) -> None:
        if self._status(vmid, "lxc")["lxc_status"] != "running":
            raise HostControlError("Managed executor requires a running LXC")

    def _resolve_node(self) -> str:
        local = self.policy.paths.pve_local.resolve()
        nodes = self.policy.paths.pve_nodes.resolve()
        if local.parent != nodes or not local.name or not re.fullmatch(r"[A-Za-z0-9._-]+", local.name):
            raise HostControlError("Cannot resolve local PVE node")
        return local.name

    def _run(
        self,
        argv: list[str],
        *,
        timeout: int,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        completed = self.runner(
            argv,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
            shell=False,
        )
        if check and completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "command failed")[-1000:]
            raise HostControlError(detail)
        return completed


def owned_snapshot(name: str, vmid: int) -> bool:
    return parse_snapshot(name, vmid) is not None


def parse_snapshot(name: str, vmid: int) -> dict[str, str] | None:
    match = SNAPSHOT_RE.fullmatch(str(name))
    if not match or int(match.group("vmid")) != int(vmid):
        return None
    parsed = match.groupdict()
    parsed["kind"] = {
        "pre": "pre-update",
        "man": "manual",
    }.get(parsed["kind"], parsed["kind"])
    return parsed


def run_forced_command(command: str, controller: HostController) -> int:
    parts = str(command or "").split()
    if len(parts) not in {2, 3}:
        raise HostControlError("Expected action vmid [snapshot]")
    action, raw_vmid = parts[:2]
    if not raw_vmid.isdigit():
        raise HostControlError("Invalid VMID")
    argument = parts[2] if len(parts) == 3 else None
    normalized = ALIASES.get(action, action)
    if normalized in set(MANAGED_ACTIONS) | {"capabilities"}:
        return controller.managed_passthrough(action, int(raw_vmid))
    result = controller.execute(action, int(raw_vmid), argument)
    print(json.dumps({"ok": True, "data": result}, separators=(",", ":")))
    return 0


def _read_vmids(path: Path) -> set[int]:
    if not path.is_file():
        raise HostControlError(f"Missing allowlist: {path}")
    values: set[int] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if value:
            if not value.isdigit() or not 1 <= int(value) <= 999999:
                raise HostControlError(f"Invalid VMID in {path}")
            values.add(int(value))
    return values


def _read_resource_types(path: Path) -> dict[int, str]:
    if not path.is_file():
        raise HostControlError(f"Missing resource type map: {path}")
    values: dict[int, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 2 or not parts[0].isdigit() or parts[1] not in RESOURCE_TYPES:
            raise HostControlError("Invalid resource type map")
        vmid = int(parts[0])
        if vmid in values:
            raise HostControlError("Duplicate resource type mapping")
        values[vmid] = parts[1]
    return values


def _json_object(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _json_list(raw: str) -> list[dict[str, Any]]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _same_vmid(value: Any, vmid: int) -> bool:
    try:
        return int(value) == int(vmid)
    except (TypeError, ValueError):
        return False


def _usage_share(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and 0 <= parsed <= 1 else None


def _snapshot_created_at(item: dict[str, Any], parsed: dict[str, str] | None) -> str | None:
    try:
        if item.get("snaptime") is not None:
            return datetime.fromtimestamp(int(item["snaptime"]), UTC).replace(
                microsecond=0
            ).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        pass
    return _name_created_at(parsed) if parsed else None


def _name_created_at(parsed: dict[str, str]) -> str:
    return datetime.strptime(parsed["stamp"], "%Y%m%dT%H%M%SZ").replace(
        tzinfo=UTC
    ).isoformat()


def _description_job_id(value: Any) -> str | None:
    match = re.search(r"(?:^|;)source_job_id=([a-f0-9]{8,64})(?:;|$)", str(value or ""))
    return match.group(1) if match else None


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--forced-command", required=True)
    args = parser.parse_args()
    try:
        return run_forced_command(
            args.forced_command,
            HostController(HostPolicy()),
        )
    except (HostControlError, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)[:2000]}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
