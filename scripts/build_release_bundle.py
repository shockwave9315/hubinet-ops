from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import tarfile
from typing import Any

try:
    from scripts.release_version import SEMVER_RE, read_application_version
except ModuleNotFoundError:  # direct `python scripts/build_release_bundle.py`
    from release_version import SEMVER_RE, read_application_version


COMMIT_RE = re.compile(r"^[a-f0-9]{40}$")
UPGRADE_RE = re.compile(r"^deploy/upgrade-(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)-from-pve\.sh$")
MAX_FILES = 512
MAX_UNPACKED_BYTES = 64 * 1024 * 1024


class BuildError(ValueError):
    pass


def _candidate_files(source_root: Path, version: str) -> list[Path]:
    exact = [
        "requirements.txt",
        "config/config.example.yaml",
        "deploy/hubinet-ops.service",
        "deploy/install-agent.sh",
        f"deploy/upgrade-{version}-from-pve.sh",
        "home-assistant/packages/hubinet_ops.yaml",
        "home-assistant/dashboards/hubinet_ops.yaml",
    ]
    roots = ["app", "deploy/managed", "deploy/pve"]
    candidates = [source_root / relative for relative in exact]
    for relative in roots:
        root = source_root / relative
        if not root.is_dir():
            raise BuildError(f"Required runtime directory is missing: {relative}")
        candidates.extend(
            path
            for path in root.rglob("*")
            if not path.is_dir()
            and "__pycache__" not in path.parts
            and path.suffix.lower() not in {".pyc", ".pyo"}
        )
    scripts = source_root / "scripts"
    if scripts.is_dir():
        candidates.extend(scripts.glob("migrate_config_*.py"))
        candidates.extend(scripts.glob("validate_rollout_state_*.py"))
        candidates.extend(
            scripts / name
            for name in (
                "render_ct110_profile.py",
                "validate_managed_profiles.py",
                "validate_pve_snapshot_policy.py",
            )
        )
    return candidates


def _validated_files(source_root: Path, version: str) -> list[tuple[str, Path]]:
    source_root = source_root.resolve()
    seen: set[str] = set()
    result: list[tuple[str, Path]] = []
    total = 0
    for candidate in _candidate_files(source_root, version):
        if candidate.is_symlink():
            raise BuildError(f"Release input contains a symlink: {candidate}")
        if not candidate.is_file():
            raise BuildError(f"Required runtime file is missing: {candidate}")
        resolved = candidate.resolve()
        try:
            relative = resolved.relative_to(source_root).as_posix()
        except ValueError as exc:
            raise BuildError("Release input escapes the source tree") from exc
        path = PurePosixPath(relative)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise BuildError("Release input path is invalid")
        if UPGRADE_RE.fullmatch(relative) and relative != f"deploy/upgrade-{version}-from-pve.sh":
            continue
        if relative in seen:
            continue
        seen.add(relative)
        total += resolved.stat().st_size
        if total > MAX_UNPACKED_BYTES:
            raise BuildError("Release runtime exceeds unpacked size limit")
        result.append((relative, resolved))
    result.sort(key=lambda item: item[0])
    if len(result) > MAX_FILES:
        raise BuildError("Release runtime exceeds file count limit")
    upgrades = [relative for relative, _ in result if UPGRADE_RE.fullmatch(relative)]
    expected = f"deploy/upgrade-{version}-from-pve.sh"
    if upgrades != [expected]:
        raise BuildError("Release must contain exactly one versioned upgrade entrypoint")
    return result


def _tar_info(name: str, size: int, *, executable: bool) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mode = 0o755 if executable else 0o644
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    return info


def build_release_bundle(
    *,
    source_root: Path,
    output: Path,
    version: str,
    commit_sha: str,
    minimum_source_version: str,
) -> dict[str, Any]:
    if SEMVER_RE.fullmatch(version) is None or SEMVER_RE.fullmatch(minimum_source_version) is None:
        raise BuildError("Release versions must be stable semantic versions")
    if COMMIT_RE.fullmatch(commit_sha) is None:
        raise BuildError("Release commit must be a full lowercase SHA")
    source_root = source_root.resolve()
    actual_version = read_application_version(source_root)
    if actual_version != version:
        raise BuildError(
            f"Application version {actual_version} does not match requested release {version}"
        )
    files = _validated_files(source_root, version)
    entries: list[dict[str, Any]] = []
    payloads: list[tuple[str, bytes]] = []
    for relative, path in files:
        payload = path.read_bytes()
        entries.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
            }
        )
        payloads.append((relative, payload))
    manifest = {
        "schema_version": 1,
        "version": version,
        "tag": f"v{version}",
        "commit_sha": commit_sha,
        "minimum_source_version": minimum_source_version,
        "entrypoint": f"deploy/upgrade-{version}-from-pve.sh",
        "files": entries,
    }
    manifest_bytes = json.dumps(
        manifest, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
                for relative, payload in payloads:
                    executable = relative.endswith(".sh") or relative in {
                        "deploy/managed/hubinet-maint",
                        "deploy/pve/hubinet-ops-host",
                        "deploy/pve/hubinet-ops-self-update",
                        "deploy/pve/hubinet-ops-ct110-system-update",
                    }
                    archive.addfile(
                        _tar_info(relative, len(payload), executable=executable),
                        io.BytesIO(payload),
                    )
                archive.addfile(
                    _tar_info("release-manifest.json", len(manifest_bytes), executable=False),
                    io.BytesIO(manifest_bytes),
                )
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    checksum_path = output.with_suffix(output.suffix + ".sha256")
    checksum_path.write_text(
        f"{digest}  {output.name}\n", encoding="ascii", newline="\n"
    )
    return {
        "version": version,
        "tag": f"v{version}",
        "commit_sha": commit_sha,
        "minimum_source_version": minimum_source_version,
        "entrypoint": manifest["entrypoint"],
        "file_count": len(entries),
        "unpacked_size": sum(item["size"] for item in entries),
        "bundle_sha256": digest,
        "bundle": str(output),
        "checksum": str(checksum_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--minimum-source-version", default="0.4.3")
    args = parser.parse_args()
    result = build_release_bundle(
        source_root=args.source_root,
        output=args.output,
        version=args.version,
        commit_sha=args.commit,
        minimum_source_version=args.minimum_source_version,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
