#!/usr/bin/env python3
"""Durable PVE supervisor for Debian updates inside CT110.

Only fixed guest-helper actions are invoked.  The approved fingerprint and the
durable host job ID identify the operation; package names never become argv.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from hubinet_ops_host_control import (
    HostControlError,
    HostController,
    snapshot_identity_argument,
)
from hubinet_ops_release import (
    FINGERPRINT_RE,
    JOB_ID_RE,
    ReleaseError,
    read_marker,
    write_marker,
)

VMID = 110
SUPERVISOR_TIMEOUT_SECONDS = 7200


class SystemUpdateError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _active_job(
    database: Path,
    job_id: str,
    fingerprint: str,
) -> dict[str, Any]:
    if not JOB_ID_RE.fullmatch(job_id) or not FINGERPRINT_RE.fullmatch(fingerprint):
        raise SystemUpdateError("Invalid CT110 system update identity")
    try:
        uri = f"{database.resolve().as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=5) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT id,vmid,operation_type,argument,status,stage "
                "FROM host_jobs WHERE id=?",
                (job_id,),
            ).fetchone()
    except sqlite3.Error as exc:
        raise SystemUpdateError("CT110 host job database is unavailable") from exc
    if row is None:
        raise SystemUpdateError("CT110 system update host job does not exist")
    job = dict(row)
    if (
        job["vmid"] != VMID
        or job["operation_type"] != "ct110_system_update"
        or job["argument"] != fingerprint
        or job["status"] != "running"
        or job["stage"] not in {"launching", "executing"}
    ):
        raise SystemUpdateError("CT110 system update host job is not active")
    return job


def prepare(
    *,
    result_dir: Path,
    database: Path,
    job_id: str,
    fingerprint: str,
) -> dict[str, Any]:
    _active_job(database, job_id, fingerprint)
    marker = read_marker(result_dir, job_id)
    if (
        marker is None
        or marker.get("status") != "launching"
        or marker.get("fingerprint") != fingerprint
    ):
        raise SystemUpdateError("CT110 system update launch marker is invalid")
    now = datetime.now(UTC).replace(microsecond=0)
    running = {
        "job_id": job_id,
        "fingerprint": fingerprint,
        "status": "running",
        "started_at": marker.get("started_at") or now.isoformat(),
        "deadline_at": (
            now + timedelta(seconds=SUPERVISOR_TIMEOUT_SECONDS)
        ).isoformat(),
        "apt_started_at": None,
        "snapshot_proof": None,
        "exit_code": None,
        "error": None,
    }
    write_marker(result_dir, job_id, running)
    return running


def _guest(controller: HostController, action: str, timeout: int) -> dict[str, Any]:
    if action not in {"check-updates", "preflight", "update", "verify"}:
        raise SystemUpdateError("Unsupported CT110 guest action")
    controller._require_running(VMID)
    return controller._managed(VMID, action, timeout=timeout)


def _proof(snapshot: dict[str, Any], job_id: str) -> dict[str, Any]:
    snaptime = snapshot.get("pve_snaptime")
    if (
        snapshot.get("vmid", VMID) != VMID
        or snapshot.get("kind") != "pre-update"
        or snapshot.get("source_job_id") != job_id
        or snapshot.get("owned_by_hubinet_ops") is not True
        or isinstance(snaptime, bool)
        or not isinstance(snaptime, int)
        or snaptime <= 0
    ):
        raise SystemUpdateError("CT110 pre-update snapshot physical proof is invalid")
    return {
        "version": 3,
        "vmid": VMID,
        "snapshot_name": str(snapshot["name"]),
        "kind": "pre-update",
        "host_source_job_id": job_id,
        "pve_snaptime": snaptime,
        "physically_confirmed": True,
    }


def _redact(value: Any) -> str:
    lines: list[str] = []
    for line in str(value or "").splitlines():
        if re.search(
            r"authorization|bearer|token|password|webhook|private[-_ ]?key",
            line,
            re.IGNORECASE,
        ):
            lines.append("[redacted sensitive output]")
        else:
            lines.append(line)
    return ("\n".join(lines).strip() or "CT110 system update failed")[-4096:]


def _health_failure(error: Exception) -> bool:
    message = str(error).lower()
    return (
        ("health" in message or "hubinet-ops.service" in message)
        and "apt-get check" not in message
        and "dpkg audit" not in message
        and "final apt scan" not in message
    )


def run(
    *,
    result_dir: Path,
    database: Path,
    job_id: str,
    fingerprint: str,
    controller: HostController | None = None,
    automatic_rollback: bool = False,
) -> int:
    controller = controller or HostController()
    _active_job(database, job_id, fingerprint)
    marker = read_marker(result_dir, job_id)
    if (
        marker is None
        or marker.get("status") != "running"
        or marker.get("fingerprint") != fingerprint
        or marker.get("apt_started_at") is not None
    ):
        raise SystemUpdateError(
            "CT110 supervisor is not at the one-shot pre-mutation boundary"
        )
    proof: dict[str, Any] | None = None
    update: dict[str, Any] | None = None
    try:
        scan = controller.execute("ct110-system-scan", VMID)
        if scan.get("fingerprint") != fingerprint:
            raise SystemUpdateError(
                "CT110 system update fingerprint changed before mutation"
            )
        preflight = _guest(controller, "preflight", 700)
        updates = preflight.get("updates")
        normalized = controller.execute("ct110-system-scan", VMID)
        if (
            not isinstance(updates, dict)
            or normalized.get("fingerprint") != fingerprint
        ):
            raise SystemUpdateError(
                "CT110 preflight fingerprint changed before snapshot"
            )
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        snapshot = controller.execute(
            "snapshot-create",
            VMID,
            f"hubinet-ops-110-pre-{stamp}",
            source_job_id=job_id,
        )
        proof = _proof(snapshot, job_id)
        marker = {
            **marker,
            "snapshot_proof": proof,
            "apt_started_at": None,
        }
        write_marker(result_dir, job_id, marker)

        post_snapshot = controller.execute("ct110-system-scan", VMID)
        if post_snapshot.get("fingerprint") != fingerprint:
            raise SystemUpdateError(
                "CT110 system state changed during snapshot creation"
            )

        marker = {
            **marker,
            "apt_started_at": utc_now(),
        }
        # This durable write is the one-shot APT mutation boundary.  A retry
        # after this point is rejected even if the previous process vanished.
        write_marker(result_dir, job_id, marker)
        update = _guest(controller, "update", 4500)
        try:
            verification = _guest(controller, "verify", 900)
        except Exception as exc:
            if automatic_rollback and proof is not None and _health_failure(exc):
                identity = snapshot_identity_argument(
                    vmid=VMID,
                    snapshot_name=str(proof["snapshot_name"]),
                    kind="pre-update",
                    expected_source_job_id=str(proof["host_source_job_id"]),
                    expected_pve_snaptime=int(proof["pve_snaptime"]),
                )
                rollback = controller.execute(
                    "snapshot-rollback", VMID, identity, source_job_id=job_id
                )
                raise SystemUpdateError(
                    f"CT110 health verification failed; automatic rollback completed: {rollback}"
                ) from exc
            raise
        required = {
            "apt_check_ok": verification.get("apt_check_ok") is True,
            "dpkg_audit_ok": verification.get("dpkg_audit_ok") is True,
            "service_active": verification.get("service_active") is True,
            "health_endpoint_ok": verification.get("health_endpoint_ok") is True,
            "final_apt_scan_ok": verification.get("final_apt_scan_ok") is True,
        }
        if not all(required.values()):
            raise SystemUpdateError(
                "CT110 final apt, dpkg, service, health, or scan verification failed"
            )
        terminal = {
            **marker,
            "status": "succeeded",
            "finished_at": utc_now(),
            "exit_code": 0,
            "error": None,
            "plan_fingerprint": fingerprint,
            "snapshot_proof": proof,
            "update": update,
            "verification": {**verification, **required},
        }
        write_marker(result_dir, job_id, terminal)
        return 0
    except Exception as exc:
        failed = {
            **(read_marker(result_dir, job_id) or marker),
            "status": "failed",
            "finished_at": utc_now(),
            "exit_code": 1,
            "error": _redact(exc),
            "plan_fingerprint": fingerprint,
            "snapshot_proof": proof,
            "update": update,
            "manual_intervention_required": True,
        }
        write_marker(result_dir, job_id, failed)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("prepare", "run"))
    parser.add_argument("--result-dir", type=Path, required=True)
    parser.add_argument("--job-database", type=Path, required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--fingerprint", required=True)
    parser.add_argument("--automatic-rollback", action="store_true")
    args = parser.parse_args()
    kwargs = {
        "result_dir": args.result_dir,
        "database": args.job_database,
        "job_id": args.job_id,
        "fingerprint": args.fingerprint,
    }
    if args.command == "prepare":
        prepare(**kwargs)
        return 0
    return run(**kwargs, automatic_rollback=args.automatic_rollback)


if __name__ == "__main__":
    raise SystemExit(main())
