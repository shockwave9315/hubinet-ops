#!/usr/bin/env python3
"""Read-only staged-release inspection and durable self-update supervision."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

FINGERPRINT_RE = re.compile(r"^[a-f0-9]{64}$")
JOB_ID_RE = re.compile(r"^[a-f0-9]{8,64}$")
UPGRADE_RE = re.compile(
    r"^deploy/upgrade-(?P<version>[0-9]+\.[0-9]+\.[0-9]+)-from-pve\.sh$"
)
TERMINAL_MARKER_STATUSES = {"succeeded", "failed"}
MAX_RELEASE_FILES = 20_000
MAX_RELEASE_BYTES = 2 * 1024 * 1024 * 1024
SUPERVISOR_TIMEOUT_SECONDS = 7200
GITHUB_REPOSITORY = "shockwave9315/hubinet-ops"
GITHUB_API_URL = (
    f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/latest"
)
ALLOWED_GITHUB_HOSTS = frozenset(
    {
        "api.github.com",
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
    }
)
SEMVER_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
COMMIT_RE = re.compile(r"^[a-f0-9]{40}$")
MANIFEST_NAME = "release-manifest.json"
STAGED_METADATA_NAME = "release-staging.json"
DEFAULT_BUNDLE_BYTES = 128 * 1024 * 1024
DEFAULT_UNPACKED_BYTES = 512 * 1024 * 1024
DEFAULT_RELEASE_FILES = 20_000
MAX_REDIRECTS = 3


class ReleaseError(RuntimeError):
    pass


class ReleaseHttpError(ReleaseError):
    def __init__(self, message: str, *, status: int) -> None:
        super().__init__(message)
        self.status = status


class ReleaseTransport(Protocol):
    def get_json(self, url: str, *, max_bytes: int) -> dict[str, Any]: ...

    def get_bytes(self, url: str, *, max_bytes: int) -> bytes: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def validate_redirect_chain(urls: list[str]) -> None:
    if not urls or len(urls) - 1 > MAX_REDIRECTS:
        raise ReleaseError("GitHub download redirect limit exceeded")
    for value in urls:
        parsed = urlsplit(value)
        if parsed.scheme != "https":
            raise ReleaseError("GitHub release downloads require HTTPS")
        if parsed.hostname not in ALLOWED_GITHUB_HOSTS:
            raise ReleaseError("GitHub release redirect host is not allowed")
        if parsed.username is not None or parsed.password is not None:
            raise ReleaseError("GitHub release URL must not contain credentials")


class GitHubHttpTransport:
    def __init__(self, *, timeout_seconds: int = 30) -> None:
        self.timeout_seconds = max(1, min(int(timeout_seconds), 120))
        self._opener = build_opener(_NoRedirect())

    def get_json(self, url: str, *, max_bytes: int) -> dict[str, Any]:
        raw = self.get_bytes(url, max_bytes=max_bytes)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReleaseError("GitHub API returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise ReleaseError("GitHub API response must be an object")
        return value

    def get_bytes(self, url: str, *, max_bytes: int) -> bytes:
        current = url
        chain = [current]
        validate_redirect_chain(chain)
        for _attempt in range(MAX_REDIRECTS + 1):
            request = Request(
                current,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "hubinet-ops-hostd/0.4.3",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                method="GET",
            )
            try:
                response = self._opener.open(
                    request, timeout=self.timeout_seconds
                )
            except HTTPError as exc:
                if exc.code in {301, 302, 303, 307, 308}:
                    location = exc.headers.get("Location")
                    if not location:
                        raise ReleaseError("GitHub redirect has no destination") from exc
                    current = urljoin(current, location)
                    chain.append(current)
                    validate_redirect_chain(chain)
                    continue
                if exc.code == 429 or (
                    exc.code == 403 and exc.headers.get("X-RateLimit-Remaining") == "0"
                ):
                    raise ReleaseHttpError(
                        "GitHub API rate limit exceeded", status=exc.code
                    ) from exc
                raise ReleaseHttpError(
                    f"GitHub request failed with HTTP {exc.code}", status=exc.code
                ) from exc
            except TimeoutError as exc:
                raise ReleaseError("GitHub release request timed out") from exc
            except URLError as exc:
                if isinstance(exc.reason, TimeoutError):
                    raise ReleaseError("GitHub release request timed out") from exc
                raise ReleaseError("GitHub release service is unavailable") from exc
            with response:
                declared = response.headers.get("Content-Length")
                if declared and int(declared) > max_bytes:
                    raise ReleaseError("Download exceeds configured size limit")
                payload = response.read(max_bytes + 1)
                if len(payload) > max_bytes:
                    raise ReleaseError("Download exceeds configured size limit")
                return payload
        raise ReleaseError("GitHub download redirect limit exceeded")


def _version(value: Any, *, field: str = "version") -> tuple[int, int, int]:
    match = SEMVER_RE.fullmatch(str(value or ""))
    if match is None:
        raise ReleaseError(f"Invalid {field}")
    return tuple(int(match.group(index)) for index in (1, 2, 3))


class GitHubReleaseDiscovery:
    def __init__(self, *, transport: ReleaseTransport | None = None) -> None:
        self.transport = transport or GitHubHttpTransport()

    def check(self, current_version: str) -> dict[str, Any]:
        current = _version(current_version, field="current version")
        try:
            metadata = self.transport.get_json(GITHUB_API_URL, max_bytes=1024 * 1024)
        except ReleaseHttpError as exc:
            if exc.status == 404:
                return {
                    "status": "no_release_published",
                    "current_version": current_version,
                }
            raise
        if metadata.get("draft") is not False or metadata.get("prerelease") is not False:
            raise ReleaseError("Latest GitHub release is not stable")
        tag = str(metadata.get("tag_name") or "")
        if not tag.startswith("v"):
            raise ReleaseError("Invalid release tag")
        latest_text = tag[1:]
        latest = _version(latest_text, field="release version")
        if tag != f"v{latest_text}":
            raise ReleaseError("Invalid release tag")
        if latest < current:
            raise ReleaseError("Hubinet Ops release downgrade is blocked")
        if latest == current:
            return {
                "status": "up_to_date",
                "current_version": current_version,
                "latest_version": latest_text,
            }
        commit = str(metadata.get("target_commitish") or "").lower()
        if COMMIT_RE.fullmatch(commit) is None:
            raise ReleaseError("Release commit SHA is invalid")
        try:
            published = datetime.fromisoformat(
                str(metadata.get("published_at") or "").replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise ReleaseError("Release published timestamp is invalid") from exc
        if published.tzinfo is None:
            raise ReleaseError("Release published timestamp is invalid")
        bundle_name = f"hubinet-ops-{latest_text}.tar.gz"
        checksum_name = f"{bundle_name}.sha256"
        assets = metadata.get("assets")
        if not isinstance(assets, list):
            raise ReleaseError("Release asset list is invalid")
        by_name: dict[str, dict[str, Any]] = {}
        for item in assets:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                if item["name"] in by_name:
                    raise ReleaseError("Release contains duplicate assets")
                by_name[item["name"]] = item
        if set(by_name) & {bundle_name, checksum_name} != {bundle_name, checksum_name}:
            raise ReleaseError("Required release asset is missing")
        bundle = by_name[bundle_name]
        checksum = by_name[checksum_name]
        bundle_url = str(bundle.get("browser_download_url") or "")
        checksum_url = str(checksum.get("browser_download_url") or "")
        validate_redirect_chain([bundle_url])
        validate_redirect_chain([checksum_url])
        # Retrieve checksum optionally; if unavailable, skip bundle_sha256
        try:
            checksum_raw = self.transport.get_bytes(checksum_url, max_bytes=4096)
            checksum_text = checksum_raw.decode("ascii")
            match = re.fullmatch(fr"([a-f0-9]{{64}})  {re.escape(bundle_name)}\n?", checksum_text)
            if match is None:
                raise ReleaseError("Release SHA-256 file is invalid")
            bundle_sha256 = match.group(1)
        except Exception:
            bundle_sha256 = None
        raw_size = bundle.get("size")
        if isinstance(raw_size, bool) or not isinstance(raw_size, int) or raw_size <= 0:
            raise ReleaseError("Release asset size is invalid")
        identity = {
            "version": latest_text,
            "tag": tag,
            "commit_sha": commit,
            "published_at": published.astimezone(UTC).replace(microsecond=0).isoformat(),
            "asset_name": bundle_name,
            "checksum_asset_name": checksum_name,
            "size": raw_size,
        }
        fingerprint = _release_identity_fingerprint(identity)
        result = {
            "status": "update_available",
            "current_version": current_version,
            "latest_version": latest_text,
            **identity,
            "fingerprint": fingerprint,
            "artifact_verification": "not_downloaded",
            "_bundle_url": bundle_url,
            "_checksum_url": checksum_url,
        }
        if bundle_sha256 is not None:
            result["bundle_sha256"] = bundle_sha256
        return result


class ReleaseStager:
    def __init__(
        self,
        *,
        transport: ReleaseTransport | None = None,
        max_bundle_bytes: int = DEFAULT_BUNDLE_BYTES,
        max_unpacked_bytes: int = DEFAULT_UNPACKED_BYTES,
        max_files: int = DEFAULT_RELEASE_FILES,
    ) -> None:
        self.transport = transport or GitHubHttpTransport()
        self.max_bundle_bytes = max(1, int(max_bundle_bytes))
        self.max_unpacked_bytes = max(1, int(max_unpacked_bytes))
        self.max_files = max(1, int(max_files))

    def stage(
        self,
        release: dict[str, Any],
        *,
        current_version: str,
        destination: Path,
    ) -> dict[str, Any]:
        if release.get("status") != "update_available":
            raise ReleaseError("Release has nothing to stage")
        expected_fingerprint = str(release.get("fingerprint") or "")
        if FINGERPRINT_RE.fullmatch(expected_fingerprint) is None:
            raise ReleaseError("Release identity fingerprint is invalid")
        bundle_url = str(release.get("_bundle_url") or "")
        checksum_url = str(release.get("_checksum_url") or "")
        validate_redirect_chain([bundle_url])
        validate_redirect_chain([checksum_url])
        checksum_raw = self.transport.get_bytes(checksum_url, max_bytes=4096)
        try:
            checksum_text = checksum_raw.decode("ascii")
        except UnicodeDecodeError as exc:
            raise ReleaseError("Release SHA-256 file is invalid") from exc
        expected_name = str(release["asset_name"])
        match = re.fullmatch(
            rf"([a-f0-9]{{64}})  {re.escape(expected_name)}\n?",
            checksum_text,
        )
        if match is None:
            raise ReleaseError("Release SHA-256 file is invalid")
        bundle = self.transport.get_bytes(
            bundle_url, max_bytes=self.max_bundle_bytes
        )
        bundle_sha = hashlib.sha256(bundle).hexdigest()
        if bundle_sha != match.group(1):
            raise ReleaseError("Release bundle SHA-256 mismatch")

        parent = destination.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if (
            os.name == "posix"
            and hasattr(os, "geteuid")
            and os.geteuid() == 0
        ):
            parent_stat = parent.stat()
            if parent_stat.st_uid != 0 or parent_stat.st_mode & 0o022:
                raise ReleaseError(
                    "Release staging parent must be root-owned and not writable"
                )
        temporary = Path(tempfile.mkdtemp(prefix=".release-", dir=parent))
        try:
            manifest = self._extract_and_verify(
                bundle,
                temporary,
                release=release,
                current_version=current_version,
            )
            staging_record = {
                "schema_version": 1,
                "version": manifest["version"],
                "tag": manifest["tag"],
                "commit_sha": manifest["commit_sha"],
                "published_at": release["published_at"],
                "asset_name": release["asset_name"],
                "size": release["size"],
                "fingerprint": expected_fingerprint,
                "bundle_sha256": bundle_sha,
                "artifact_verification": "verified",
            }
            staging_target = temporary / STAGED_METADATA_NAME
            descriptor = os.open(
                staging_target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(
                    staging_record,
                    output,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            previous = destination.with_name(destination.name + ".previous")
            moved_previous = False
            if destination.exists():
                if destination.is_symlink():
                    raise ReleaseError("Staging destination must not be a symbolic link")
                if previous.exists():
                    if previous.is_symlink() or not previous.is_dir():
                        raise ReleaseError("Previous staging destination is unsafe")
                    shutil.rmtree(previous)
                os.replace(destination, previous)
                moved_previous = True
            try:
                os.replace(temporary, destination)
            except Exception:
                if moved_previous and not destination.exists():
                    os.replace(previous, destination)
                raise
            return {
                "status": "staged",
                "version": manifest["version"],
                "tag": manifest["tag"],
                "commit_sha": manifest["commit_sha"],
                "minimum_source_version": manifest["minimum_source_version"],
                "release_id": (
                    f"hubinet-ops-{manifest['version']}-{bundle_sha[:16]}"
                ),
                "fingerprint": expected_fingerprint,
                "bundle_sha256": bundle_sha,
                "file_count": len(manifest["files"]),
                "total_bytes": sum(item["size"] for item in manifest["files"]),
                "artifact_verification": "verified",
            }
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    def _extract_and_verify(
        self,
        bundle: bytes,
        root: Path,
        *,
        release: dict[str, Any],
        current_version: str,
    ) -> dict[str, Any]:
        try:
            archive = tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz")
        except (tarfile.TarError, OSError) as exc:
            raise ReleaseError("Release bundle archive is invalid") from exc
        with archive:
            members = archive.getmembers()
            names: set[str] = set()
            files: dict[str, tarfile.TarInfo] = {}
            total = 0
            for member in members:
                name = member.name
                path = PurePosixPath(name)
                if (
                    not name
                    or "\\" in name
                    or path.is_absolute()
                    or any(part in {"", ".", ".."} for part in path.parts)
                ):
                    raise ReleaseError("Release archive path is unsafe")
                if name in names:
                    raise ReleaseError("Release archive contains duplicate paths")
                names.add(name)
                if member.isdir():
                    continue
                if not member.isreg():
                    raise ReleaseError("Release archive link or special file is forbidden")
                files[name] = member
                total += member.size
                if len(files) > self.max_files:
                    raise ReleaseError("Release archive file count limit exceeded")
                if total > self.max_unpacked_bytes:
                    raise ReleaseError("Release archive unpacked size limit exceeded")
            manifest_member = files.get(MANIFEST_NAME)
            if manifest_member is None or manifest_member.size > 1024 * 1024:
                raise ReleaseError("Release manifest is missing or too large")
            handle = archive.extractfile(manifest_member)
            if handle is None:
                raise ReleaseError("Release manifest is unreadable")
            try:
                manifest = json.loads(handle.read().decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ReleaseError("Release manifest is invalid") from exc
            self._validate_manifest(
                manifest,
                release=release,
                current_version=current_version,
                archive_files=set(files) - {MANIFEST_NAME},
            )
            manifest_by_path = {item["path"]: item for item in manifest["files"]}
            for name, member in files.items():
                if name == MANIFEST_NAME:
                    continue
                item = manifest_by_path[name]
                source = archive.extractfile(member)
                if source is None:
                    raise ReleaseError("Release archive file is unreadable")
                data = source.read(member.size + 1)
                if len(data) != member.size or len(data) != item["size"]:
                    raise ReleaseError("Release file size does not match manifest")
                if hashlib.sha256(data).hexdigest() != item["sha256"]:
                    raise ReleaseError("Release file SHA-256 does not match manifest")
                target = root.joinpath(*PurePosixPath(name).parts)
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                descriptor = os.open(
                    target, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o755 if name == manifest["entrypoint"] else 0o644,
                )
                with os.fdopen(descriptor, "wb") as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
            manifest_target = root / MANIFEST_NAME
            descriptor = os.open(
                manifest_target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(manifest, output, sort_keys=True, separators=(",", ":"))
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            return manifest

    @staticmethod
    def _validate_manifest(
        manifest: Any,
        *,
        release: dict[str, Any],
        current_version: str,
        archive_files: set[str],
    ) -> None:
        required = {
            "schema_version", "version", "tag", "commit_sha",
            "minimum_source_version", "entrypoint", "files",
        }
        if not isinstance(manifest, dict) or set(manifest) != required:
            raise ReleaseError("Release manifest fields are invalid")
        if manifest["schema_version"] != 1:
            raise ReleaseError("Release manifest schema is invalid")
        version = str(manifest["version"])
        current = _version(current_version, field="current version")
        target = _version(version, field="manifest version")
        minimum = _version(
            manifest["minimum_source_version"], field="minimum source version"
        )
        if current < minimum:
            raise ReleaseError("Current version is below the supported source version")
        if target <= current:
            raise ReleaseError("Release reinstall or downgrade is blocked")
        if version != release.get("latest_version") or manifest["tag"] != release.get("tag"):
            raise ReleaseError("Release manifest version or tag mismatch")
        if manifest["tag"] != f"v{version}":
            raise ReleaseError("Release manifest tag is invalid")
        if (
            COMMIT_RE.fullmatch(str(manifest["commit_sha"])) is None
            or manifest["commit_sha"] != release.get("commit_sha")
        ):
            raise ReleaseError("Release manifest commit mismatch")
        entrypoint = str(manifest["entrypoint"])
        if entrypoint != f"deploy/upgrade-{version}-from-pve.sh":
            raise ReleaseError("Release manifest entrypoint is invalid")
        values = manifest["files"]
        if not isinstance(values, list):
            raise ReleaseError("Release manifest file list is invalid")
        paths: set[str] = set()
        for item in values:
            if not isinstance(item, dict) or set(item) != {"path", "sha256", "size"}:
                raise ReleaseError("Release manifest file entry is invalid")
            path = str(item["path"])
            parsed = PurePosixPath(path)
            if (
                not path
                or "\\" in path
                or parsed.is_absolute()
                or any(part in {"", ".", ".."} for part in parsed.parts)
                or path in paths
            ):
                raise ReleaseError("Release manifest file path is invalid")
            paths.add(path)
            if FINGERPRINT_RE.fullmatch(str(item["sha256"])) is None:
                raise ReleaseError("Release manifest file SHA-256 is invalid")
            if isinstance(item["size"], bool) or not isinstance(item["size"], int) or item["size"] < 0:
                raise ReleaseError("Release manifest file size is invalid")
        if paths != archive_files or entrypoint not in paths:
            raise ReleaseError("Release manifest does not match archive files")
        upgrades = [path for path in paths if UPGRADE_RE.fullmatch(path)]
        if upgrades != [entrypoint]:
            raise ReleaseError("Release must contain exactly one versioned entrypoint")


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def inspect_staged_release(root: Path) -> dict[str, Any]:
    """Re-verify and return a root-owned immutable staged release identity."""
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
    by_name = {relative: path for relative, path in files}
    try:
        manifest = json.loads(by_name[MANIFEST_NAME].read_text(encoding="utf-8"))
        staging = json.loads(
            by_name[STAGED_METADATA_NAME].read_text(encoding="utf-8")
        )
    except KeyError as exc:
        raise ReleaseError("Staged release manifest or verification record is missing") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError("Staged release metadata is invalid") from exc
    required_manifest = {
        "schema_version", "version", "tag", "commit_sha",
        "minimum_source_version", "entrypoint", "files",
    }
    if not isinstance(manifest, dict) or set(manifest) != required_manifest:
        raise ReleaseError("Staged release manifest is invalid")
    required_staging = {
        "schema_version", "version", "tag", "commit_sha", "published_at",
        "asset_name", "size", "fingerprint", "bundle_sha256",
        "artifact_verification",
    }
    if not isinstance(staging, dict) or set(staging) != required_staging:
        raise ReleaseError("Staged release verification record is invalid")
    version = str(manifest.get("version") or "")
    expected_asset_name = f"hubinet-ops-{version}.tar.gz"
    try:
        published_at = datetime.fromisoformat(
            str(staging.get("published_at") or "").replace("Z", "+00:00")
        )
        minimum_source = _version(
            str(manifest.get("minimum_source_version") or ""),
            field="minimum source version",
        )
    except (ValueError, TypeError) as exc:
        raise ReleaseError("Staged release identity is invalid") from exc
    if (
        manifest.get("schema_version") != 1
        or _version(version, field="staged version") < (0, 0, 1)
        or manifest.get("tag") != f"v{version}"
        or COMMIT_RE.fullmatch(str(manifest.get("commit_sha") or "")) is None
        or manifest.get("entrypoint") != f"deploy/upgrade-{version}-from-pve.sh"
        or staging.get("schema_version") != 1
        or staging.get("version") != version
        or staging.get("tag") != manifest.get("tag")
        or staging.get("commit_sha") != manifest.get("commit_sha")
        or staging.get("artifact_verification") != "verified"
        or published_at.tzinfo is None
        or staging.get("asset_name") != expected_asset_name
        or isinstance(staging.get("size"), bool)
        or not isinstance(staging.get("size"), int)
        or int(staging["size"]) <= 0
        or minimum_source > _version(version, field="staged version")
        or FINGERPRINT_RE.fullmatch(str(staging.get("fingerprint") or "")) is None
        or FINGERPRINT_RE.fullmatch(str(staging.get("bundle_sha256") or "")) is None
    ):
        raise ReleaseError("Staged release identity is invalid")
    identity = {
        "version": version,
        "tag": manifest["tag"],
        "commit_sha": manifest["commit_sha"],
        "published_at": staging["published_at"],
        "asset_name": staging["asset_name"],
        "checksum_asset_name": f"{staging['asset_name']}.sha256",
        "size": staging["size"],
    }
    if _release_identity_fingerprint(identity) != staging["fingerprint"]:
        raise ReleaseError("Staged release fingerprint does not match its identity")
    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise ReleaseError("Staged release file manifest is invalid")
    expected: dict[str, dict[str, Any]] = {}
    for item in entries:
        if not isinstance(item, dict) or set(item) != {"path", "sha256", "size"}:
            raise ReleaseError("Staged release file manifest is invalid")
        relative = str(item.get("path") or "")
        path = PurePosixPath(relative)
        if (
            not relative
            or "\\" in relative
            or path.is_absolute()
            or any(part in {"", ".", ".."} for part in path.parts)
            or relative in expected
            or FINGERPRINT_RE.fullmatch(str(item.get("sha256") or "")) is None
            or isinstance(item.get("size"), bool)
            or not isinstance(item.get("size"), int)
            or int(item["size"]) < 0
        ):
            raise ReleaseError("Staged release file manifest is invalid")
        expected[relative] = item
    actual = set(by_name) - {MANIFEST_NAME, STAGED_METADATA_NAME}
    if actual != set(expected):
        raise ReleaseError("Staged release files do not match the manifest")
    for relative, item in expected.items():
        path = by_name[relative]
        if path.stat().st_size != item["size"]:
            raise ReleaseError("Staged release file size mismatch")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        if digest.hexdigest() != item["sha256"]:
            raise ReleaseError("Staged release file checksum mismatch")
    upgrades = [relative for relative in expected if UPGRADE_RE.fullmatch(relative)]
    if upgrades != [manifest["entrypoint"]]:
        raise ReleaseError("Staged release must contain exactly one versioned upgrade entrypoint")
    return {
        "version": version,
        "tag": manifest["tag"],
        "commit_sha": manifest["commit_sha"],
        "published_at": staging["published_at"],
        "minimum_source_version": manifest["minimum_source_version"],
        "release_id": f"hubinet-ops-{version}-{str(staging['fingerprint'])[:16]}",
        "fingerprint": staging["fingerprint"],
        "bundle_sha256": staging["bundle_sha256"],
        "artifact_verification": "verified",
        "file_count": len(expected),
        "total_bytes": sum(int(item["size"]) for item in expected.values()),
        "upgrade_path": str(root / manifest["entrypoint"]),
    }


def public_release(release: dict[str, Any]) -> dict[str, Any]:
    return {
        key: release[key]
        for key in (
            "version", "tag", "commit_sha", "published_at",
            "minimum_source_version", "release_id", "fingerprint",
            "bundle_sha256", "artifact_verification", "file_count", "total_bytes",
        )
    }


def _release_identity_fingerprint(identity: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


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
    if status not in {"launching", "running", *TERMINAL_MARKER_STATUSES}:
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


def assert_active_self_update_job(
    *,
    database: Path,
    job_id: str,
    expected_fingerprint: str,
    require_launch_window: bool,
) -> dict[str, Any]:
    if not JOB_ID_RE.fullmatch(job_id):
        raise ReleaseError("Invalid self-update job ID")
    if not FINGERPRINT_RE.fullmatch(expected_fingerprint):
        raise ReleaseError("Invalid approved release fingerprint")
    try:
        uri = f"{database.resolve().as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=5) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT id, operation_type, argument, status, stage, "
                "launching_started_at, launch_deadline_at "
                "FROM host_jobs WHERE id=?",
                (job_id,),
            ).fetchone()
    except sqlite3.Error as exc:
        raise ReleaseError("Self-update host job state is unavailable") from exc
    if row is None:
        raise ReleaseError("Self-update host job does not exist")
    job = dict(row)
    if (
        job["operation_type"] != "self_update"
        or job["argument"] != expected_fingerprint
        or job["status"] != "running"
        or job["stage"] not in {"launching", "executing"}
    ):
        raise ReleaseError("Self-update host job is no longer active")
    if require_launch_window:
        try:
            deadline = datetime.fromisoformat(str(job["launch_deadline_at"]))
        except (TypeError, ValueError) as exc:
            raise ReleaseError("Self-update launch deadline is invalid") from exc
        if deadline.tzinfo is None or deadline.astimezone(UTC) <= datetime.now(UTC):
            raise ReleaseError("Self-update launch deadline expired")
    return job


def _require_marker(
    *,
    result_dir: Path,
    job_id: str,
    expected_fingerprint: str,
    expected_status: str,
) -> dict[str, Any]:
    marker = read_marker(result_dir, job_id)
    if marker is None or marker.get("status") != expected_status:
        raise ReleaseError(
            f"Self-update {expected_status} marker is missing"
        )
    if marker.get("fingerprint") != expected_fingerprint:
        raise ReleaseError(
            "Self-update marker fingerprint does not match the approved release"
        )
    if expected_status == "launching":
        try:
            deadline = datetime.fromisoformat(str(marker.get("deadline_at")))
        except (TypeError, ValueError) as exc:
            raise ReleaseError("Self-update launch marker deadline is invalid") from exc
        if deadline.tzinfo is None or deadline.astimezone(UTC) <= datetime.now(UTC):
            raise ReleaseError("Self-update launch marker expired")
    return marker


def prepare_supervisor(
    *,
    release_root: Path,
    result_dir: Path,
    database: Path,
    job_id: str,
    expected_fingerprint: str,
) -> dict[str, Any]:
    assert_active_self_update_job(
        database=database,
        job_id=job_id,
        expected_fingerprint=expected_fingerprint,
        require_launch_window=True,
    )
    launching = _require_marker(
        result_dir=result_dir,
        job_id=job_id,
        expected_fingerprint=expected_fingerprint,
        expected_status="launching",
    )
    release = inspect_staged_release(release_root)
    if release["fingerprint"] != expected_fingerprint:
        raise ReleaseError("Staged release fingerprint changed before rollout")
    assert_active_self_update_job(
        database=database,
        job_id=job_id,
        expected_fingerprint=expected_fingerprint,
        require_launch_window=True,
    )
    _require_marker(
        result_dir=result_dir,
        job_id=job_id,
        expected_fingerprint=expected_fingerprint,
        expected_status="launching",
    )
    now = datetime.now(UTC).replace(microsecond=0)
    marker = {
        **public_release(release),
        "job_id": job_id,
        "status": "running",
        "started_at": launching.get("started_at") or now.isoformat(),
        "deadline_at": (now + timedelta(seconds=SUPERVISOR_TIMEOUT_SECONDS)).isoformat(),
        "exit_code": None,
        "error": None,
    }
    write_marker(result_dir, job_id, marker)
    return marker


def verify_supervisor_launch(
    *,
    result_dir: Path,
    database: Path,
    job_id: str,
    expected_fingerprint: str,
) -> None:
    assert_active_self_update_job(
        database=database,
        job_id=job_id,
        expected_fingerprint=expected_fingerprint,
        require_launch_window=False,
    )
    _require_marker(
        result_dir=result_dir,
        job_id=job_id,
        expected_fingerprint=expected_fingerprint,
        expected_status="running",
    )


def run_supervisor(
    *,
    release_root: Path,
    result_dir: Path,
    database: Path,
    job_id: str,
    expected_fingerprint: str,
) -> int:
    try:
        assert_active_self_update_job(
            database=database,
            job_id=job_id,
            expected_fingerprint=expected_fingerprint,
            require_launch_window=False,
        )
    except ReleaseError:
        return 125
    try:
        running = _require_marker(
            result_dir=result_dir,
            job_id=job_id,
            expected_fingerprint=expected_fingerprint,
            expected_status="running",
        )
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

    try:
        verify_supervisor_launch(
            result_dir=result_dir,
            database=database,
            job_id=job_id,
            expected_fingerprint=expected_fingerprint,
        )
    except ReleaseError:
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
        if re.search(
            r"authorization|bearer|token|password|webhook|private[-_ ]?key|mqtt",
            line,
            re.IGNORECASE,
        ):
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
        child.add_argument("--job-database", type=Path, required=True)
        child.add_argument("--job-id", required=True)
        child.add_argument("--fingerprint", required=True)
    verify = subparsers.add_parser("verify-active")
    verify.add_argument("--result-dir", type=Path, required=True)
    verify.add_argument("--job-database", type=Path, required=True)
    verify.add_argument("--job-id", required=True)
    verify.add_argument("--fingerprint", required=True)
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
            database=args.job_database,
            job_id=args.job_id,
            expected_fingerprint=args.fingerprint,
        )
        return 0
    if args.command == "run":
        return run_supervisor(
            release_root=args.release_root,
            result_dir=args.result_dir,
            database=args.job_database,
            job_id=args.job_id,
            expected_fingerprint=args.fingerprint,
        )
    if args.command == "verify-active":
        verify_supervisor_launch(
            result_dir=args.result_dir,
            database=args.job_database,
            job_id=args.job_id,
            expected_fingerprint=args.fingerprint,
        )
        return 0
    record_launch_failure(
        result_dir=args.result_dir,
        job_id=args.job_id,
        expected_fingerprint=args.fingerprint,
        exit_code=args.exit_code,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
