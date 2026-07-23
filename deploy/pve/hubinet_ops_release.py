#!/usr/bin/env python3
"""Read-only staged-release inspection and durable self-update supervision."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

FINGERPRINT_RE = re.compile(r"^[a-f0-9]{64}$")
JOB_ID_RE = re.compile(r"^[a-f0-9]{8,64}$")
UPGRADE_RE = re.compile(
    r"^deploy/upgrade-(?P<version>[0-9]+\.[0-9]+\.[0-9]+)-from-pve\.sh$"
)
TERMINAL_MARKER_STATUSES = {"succeeded", "failed"}
MAX_RELEASE_FILES = 20_000
MAX_RELEASE_BYTES = 2 * 1024 * 1024 * 1024
SUPERVISOR_TIMEOUT_SECONDS = 7200


class ReleaseError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def inspect_staged_release(root: Path) -> dict[str, Any]:
    """Return a stable identity for a root-owned staged release without mutation."""
    if root.is_symlink():
        raise ReleaseError("Approved release root must not be a symbolic link")
    root = root.resolve()
    if not root.is_dir():
        raise ReleaseError("No approved Hubinet Ops release is staged")
    enforce_root_ownership = (
        os.name == "posix"
        and hasattr(os, "geteuid")
        and os.geteuid() == 0
    )
    if enforce_root_ownership:
        root_stat = root.stat()
        if root_stat.st_uid != 0 or root_stat.st_mode & 0o022:
            raise ReleaseError("Approved release root must be root-owned and not writable")

    files: list[tuple[str, Path]] = []
    total_bytes = 0
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ReleaseError("Staged release must not contain symbolic links")
        if not path.is_file():
            continue
        if enforce_root_ownership:
            path_stat = path.stat()
            if path_stat.st_uid != 0 or path_stat.st_mode & 0o022:
                raise ReleaseError(
                    "Approved release files must be root-owned and not group/world writable"
                )
        relative = path.relative_to(root).as_posix()
        size = path.stat().st_size
        total_bytes += size
        if len(files) >= MAX_RELEASE_FILES or total_bytes > MAX_RELEASE_BYTES:
            raise ReleaseError("Staged release exceeds inspection limits")
        files.append((relative, path))

    files.sort(key=lambda item: item[0])
    upgrades = [
        (match, path)
        for relative, path in files
        if (match := UPGRADE_RE.fullmatch(relative)) is not None
    ]
    if len(upgrades) != 1:
        raise ReleaseError("Staged release must contain exactly one versioned upgrade entrypoint")

    digest = hashlib.sha256()
    digest.update(b"hubinet-ops-release-v1\0")
    for relative, path in files:
        encoded = relative.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(path.stat().st_size.to_bytes(8, "big"))
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)

    fingerprint = digest.hexdigest()
    version = upgrades[0][0].group("version")
    return {
        "version": version,
        "release_id": f"hubinet-ops-{version}-{fingerprint[:16]}",
        "fingerprint": fingerprint,
        "file_count": len(files),
        "total_bytes": total_bytes,
        "upgrade_path": str(upgrades[0][1]),
    }


def public_release(release: dict[str, Any]) -> dict[str, Any]:
    return {
        key: release[key]
        for key in ("version", "release_id", "fingerprint", "file_count", "total_bytes")
    }


def marker_path(result_dir: Path, job_id: str) -> Path:
    if not JOB_ID_RE.fullmatch(job_id):
        raise ReleaseError("Invalid self-update job ID")
    return result_dir / f"{job_id}.json"


def read_marker(result_dir: Path, job_id: str) -> dict[str, Any] | None:
    path = marker_path(result_dir, job_id)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError("Self-update supervisor marker is invalid") from exc
    if not isinstance(payload, dict):
        raise ReleaseError("Self-update supervisor marker is invalid")
    status = str(payload.get("status") or "")
    if status not in {"running", *TERMINAL_MARKER_STATUSES}:
        raise ReleaseError("Self-update supervisor marker has invalid status")
    fingerprint = str(payload.get("fingerprint") or "")
    if not FINGERPRINT_RE.fullmatch(fingerprint):
        raise ReleaseError("Self-update supervisor marker has invalid fingerprint")
    if status in TERMINAL_MARKER_STATUSES:
        exit_code = payload.get("exit_code")
        if isinstance(exit_code, bool) or not isinstance(exit_code, int):
            raise ReleaseError("Self-update supervisor marker has invalid exit code")
        if status == "succeeded" and exit_code != 0:
            raise ReleaseError("Successful self-update marker has a non-zero exit code")
        if status == "failed" and exit_code == 0:
            raise ReleaseError("Failed self-update marker has a zero exit code")
    return payload


def write_marker(result_dir: Path, job_id: str, payload: dict[str, Any]) -> Path:
    path = marker_path(result_dir, job_id)
    result_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = result_dir / f".{job_id}.{os.getpid()}.tmp"
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return path


def remove_marker(result_dir: Path, job_id: str) -> None:
    marker_path(result_dir, job_id).unlink(missing_ok=True)


def prepare_supervisor(
    *,
    release_root: Path,
    result_dir: Path,
    job_id: str,
    expected_fingerprint: str,
) -> dict[str, Any]:
    if not FINGERPRINT_RE.fullmatch(expected_fingerprint):
        raise ReleaseError("Invalid approved release fingerprint")
    release = inspect_staged_release(release_root)
    if release["fingerprint"] != expected_fingerprint:
        raise ReleaseError("Staged release fingerprint changed before rollout")
    now = datetime.now(UTC).replace(microsecond=0)
    marker = {
        **public_release(release),
        "job_id": job_id,
        "status": "running",
        "started_at": now.isoformat(),
        "deadline_at": (now + timedelta(seconds=SUPERVISOR_TIMEOUT_SECONDS)).isoformat(),
        "exit_code": None,
        "error": None,
    }
    write_marker(result_dir, job_id, marker)
    return marker


def run_supervisor(
    *,
    release_root: Path,
    result_dir: Path,
    job_id: str,
    expected_fingerprint: str,
) -> int:
    try:
        running = read_marker(result_dir, job_id)
        if running is None or running.get("status") != "running":
            raise ReleaseError("Self-update running marker is missing")
        release = inspect_staged_release(release_root)
        if release["fingerprint"] != expected_fingerprint:
            raise ReleaseError("Staged release fingerprint changed before rollout")
    except ReleaseError as exc:
        _write_terminal_failure(
            result_dir=result_dir,
            job_id=job_id,
            expected_fingerprint=expected_fingerprint,
            exit_code=125,
            error=str(exc),
        )
        return 125

    timed_out = False
    output = ""
    try:
        completed = subprocess.run(
            ["/usr/bin/bash", str(release["upgrade_path"])],
            text=True,
            capture_output=True,
            timeout=SUPERVISOR_TIMEOUT_SECONDS,
            check=False,
            shell=False,
        )
        exit_code = int(completed.returncode)
        output = f"{completed.stdout}\n{completed.stderr}"
    except subprocess.TimeoutExpired as exc:
        exit_code = 124
        timed_out = True
        output = f"{exc.stdout or ''}\n{exc.stderr or ''}"
    error = None
    if exit_code != 0:
        detail = _safe_output_tail(output)
        error = (
            f"Self-update rollout timed out: {detail}"
            if timed_out
            else detail
        )
    finished = {
        **public_release(release),
        "job_id": job_id,
        "status": "succeeded" if exit_code == 0 else "failed",
        "started_at": running.get("started_at"),
        "finished_at": utc_now(),
        "exit_code": exit_code,
        "error": error,
    }
    write_marker(result_dir, job_id, finished)
    return exit_code


def record_launch_failure(
    *,
    result_dir: Path,
    job_id: str,
    expected_fingerprint: str,
    exit_code: int,
) -> None:
    _write_terminal_failure(
        result_dir=result_dir,
        job_id=job_id,
        expected_fingerprint=expected_fingerprint,
        exit_code=exit_code or 126,
        error=f"Failed to launch self-update supervisor (exit code {exit_code})",
    )


def _write_terminal_failure(
    *,
    result_dir: Path,
    job_id: str,
    expected_fingerprint: str,
    exit_code: int,
    error: str,
) -> None:
    existing = read_marker(result_dir, job_id) or {}
    write_marker(
        result_dir,
        job_id,
        {
            "version": existing.get("version", "unknown"),
            "release_id": existing.get("release_id", "unknown"),
            "fingerprint": expected_fingerprint,
            "file_count": existing.get("file_count"),
            "total_bytes": existing.get("total_bytes"),
            "job_id": job_id,
            "status": "failed",
            "started_at": existing.get("started_at"),
            "finished_at": utc_now(),
            "exit_code": int(exit_code),
            "error": str(error)[:4096],
        },
    )


def _safe_output_tail(value: Any) -> str:
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = str(value or "")
    text = text[-8192:]
    lines = []
    for line in text.splitlines():
        if re.search(r"authorization|bearer|token|password|webhook", line, re.IGNORECASE):
            lines.append("[redacted sensitive output]")
        else:
            lines.append(line)
    return ("\n".join(lines).strip() or "Self-update rollout failed")[-4096:]


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("prepare", "run"):
        child = subparsers.add_parser(command)
        child.add_argument("--release-root", type=Path, required=True)
        child.add_argument("--result-dir", type=Path, required=True)
        child.add_argument("--job-id", required=True)
        child.add_argument("--fingerprint", required=True)
    failed = subparsers.add_parser("launch-failed")
    failed.add_argument("--result-dir", type=Path, required=True)
    failed.add_argument("--job-id", required=True)
    failed.add_argument("--fingerprint", required=True)
    failed.add_argument("--exit-code", type=int, required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        prepare_supervisor(
            release_root=args.release_root,
            result_dir=args.result_dir,
            job_id=args.job_id,
            expected_fingerprint=args.fingerprint,
        )
        return 0
    if args.command == "run":
        return run_supervisor(
            release_root=args.release_root,
            result_dir=args.result_dir,
            job_id=args.job_id,
            expected_fingerprint=args.fingerprint,
        )
    record_launch_failure(
        result_dir=args.result_dir,
        job_id=args.job_id,
        expected_fingerprint=args.fingerprint,
        exit_code=args.exit_code,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
