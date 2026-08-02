from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
from typing import Any

import pytest


PVE = Path(__file__).parents[1] / "deploy" / "pve"
sys.path.insert(0, str(PVE))

from hubinet_ops_release import (  # noqa: E402
    GitHubReleaseDiscovery,
    ReleaseError,
    ReleaseStager,
    _safe_output_tail,
    validate_redirect_chain,
)


COMMIT = "0123456789abcdef0123456789abcdef01234567"


class FakeTransport:
    def __init__(self, metadata: dict[str, Any], payloads: dict[str, bytes]) -> None:
        self.metadata = metadata
        self.payloads = dict(payloads)
        self.calls: list[tuple[str, str]] = []
        self.error: Exception | None = None

    def get_json(self, url: str, *, max_bytes: int) -> dict[str, Any]:
        self.calls.append(("json", url))
        if self.error:
            raise self.error
        return dict(self.metadata)

    def get_bytes(self, url: str, *, max_bytes: int) -> bytes:
        self.calls.append(("bytes", url))
        if self.error:
            raise self.error
        value = self.payloads[url]
        if len(value) > max_bytes:
            raise ReleaseError("Download exceeds configured size limit")
        return value


def _bundle(
    *,
    version: str = "0.4.4",
    tag: str | None = None,
    commit: str = COMMIT,
    minimum: str = "0.4.3",
    mutation: str | None = None,
    extra_files: int = 0,
) -> bytes:
    entrypoint = f"deploy/upgrade-{version}-from-pve.sh"
    files: dict[str, bytes] = {
        entrypoint: b"#!/usr/bin/env bash\nexit 0\n",
        "app/mqtt.py": f'VERSION = "{version}"\n'.encode(),
    }
    for index in range(extra_files):
        files[f"runtime/file-{index}.txt"] = b"x"
    entries = [
        {
            "path": path,
            "sha256": hashlib.sha256(value).hexdigest(),
            "size": len(value),
        }
        for path, value in sorted(files.items())
    ]
    manifest = {
        "schema_version": 1,
        "version": version,
        "tag": tag or f"v{version}",
        "commit_sha": commit,
        "minimum_source_version": minimum,
        "entrypoint": entrypoint,
        "files": entries,
    }
    if mutation == "manifest_unknown":
        manifest["command"] = "arbitrary"
    if mutation == "manifest_commit":
        manifest["commit_sha"] = "f" * 40
    if mutation == "manifest_entrypoint":
        manifest["entrypoint"] = "deploy/not-versioned.sh"
    manifest_bytes = json.dumps(
        manifest, sort_keys=True, separators=(",", ":")
    ).encode()
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for path, value in files.items():
            info = tarfile.TarInfo(path)
            info.size = len(value)
            info.mode = 0o755 if path == entrypoint else 0o644
            archive.addfile(info, io.BytesIO(value))
        info = tarfile.TarInfo("release-manifest.json")
        info.size = len(manifest_bytes)
        archive.addfile(info, io.BytesIO(manifest_bytes))
        if mutation == "symlink":
            link = tarfile.TarInfo("runtime/link")
            link.type = tarfile.SYMTYPE
            link.linkname = "/etc/passwd"
            archive.addfile(link)
        if mutation == "traversal":
            bad = tarfile.TarInfo("../escape")
            bad.size = 1
            archive.addfile(bad, io.BytesIO(b"x"))
        if mutation == "absolute":
            bad = tarfile.TarInfo("/escape")
            bad.size = 1
            archive.addfile(bad, io.BytesIO(b"x"))
    return stream.getvalue()


def _release_metadata(
    version: str = "0.4.4",
    *,
    tag: str | None = None,
    commit: str = COMMIT,
) -> dict[str, Any]:
    resolved_tag = tag or f"v{version}"
    bundle_name = f"hubinet-ops-{version}.tar.gz"
    return {
        "tag_name": resolved_tag,
        "target_commitish": commit,
        "draft": False,
        "prerelease": False,
        "published_at": "2026-08-02T12:00:00Z",
        "assets": [
            {
                "name": bundle_name,
                "browser_download_url": (
                    f"https://github.com/shockwave9315/hubinet-ops/releases/download/"
                    f"{resolved_tag}/{bundle_name}"
                ),
                "size": 1234,
            },
            {
                "name": bundle_name + ".sha256",
                "browser_download_url": (
                    f"https://github.com/shockwave9315/hubinet-ops/releases/download/"
                    f"{resolved_tag}/{bundle_name}.sha256"
                ),
                "size": 100,
            },
        ],
    }


def _transport_for_bundle(
    bundle: bytes,
    *,
    metadata: dict[str, Any] | None = None,
    checksum: str | None = None,
) -> tuple[FakeTransport, dict[str, Any]]:
    metadata = metadata or _release_metadata()
    assets = {item["name"]: item["browser_download_url"] for item in metadata["assets"]}
    bundle_name = next(name for name in assets if name.endswith(".tar.gz"))
    digest = checksum or hashlib.sha256(bundle).hexdigest()
    transport = FakeTransport(
        metadata,
        {
            assets[bundle_name]: bundle,
            assets[bundle_name + ".sha256"]: (
                f"{digest}  {bundle_name}\n".encode()
            ),
        },
    )
    discovery = GitHubReleaseDiscovery(transport=transport)
    return transport, discovery.check("0.4.3")


def test_same_release_is_up_to_date_and_never_downloaded() -> None:
    transport = FakeTransport(_release_metadata("0.4.4"), {})
    result = GitHubReleaseDiscovery(transport=transport).check("0.4.4")

    assert result == {
        "status": "up_to_date",
        "current_version": "0.4.4",
        "latest_version": "0.4.4",
    }
    assert [call[0] for call in transport.calls] == ["json"]


def test_new_release_returns_immutable_identity_without_user_url_input() -> None:
    transport = FakeTransport(_release_metadata(), {})
    result = GitHubReleaseDiscovery(transport=transport).check("0.4.3")

    assert result["status"] == "update_available"
    assert result["current_version"] == "0.4.3"
    assert result["latest_version"] == "0.4.4"
    assert result["tag"] == "v0.4.4"
    assert result["commit_sha"] == COMMIT
    assert result["published_at"] == "2026-08-02T12:00:00+00:00"
    assert len(result["fingerprint"]) == 64
    assert result["artifact_verification"] == "not_downloaded"


@pytest.mark.parametrize(
    ("version", "tag", "message"),
    [
        ("0.4.2", "v0.4.2", "downgrade"),
        ("0.4.4", "release-0.4.4", "tag"),
        ("0.4", "v0.4", "version"),
    ],
)
def test_invalid_or_older_release_is_blocked(
    version: str,
    tag: str,
    message: str,
) -> None:
    transport = FakeTransport(_release_metadata(version, tag=tag), {})
    with pytest.raises(ReleaseError, match=message):
        GitHubReleaseDiscovery(transport=transport).check("0.4.3")


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("manifest_unknown", "manifest"),
        ("manifest_commit", "commit"),
        ("manifest_entrypoint", "entrypoint"),
        ("symlink", "link"),
        ("traversal", "path"),
        ("absolute", "path"),
    ],
)
def test_malicious_or_mismatched_bundle_is_rejected_without_changing_staging(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    staged = tmp_path / "approved-release"
    staged.mkdir()
    (staged / "sentinel").write_text("previous-valid", encoding="utf-8")
    bundle = _bundle(mutation=mutation)
    transport, release = _transport_for_bundle(bundle)

    with pytest.raises(ReleaseError, match=message):
        ReleaseStager(transport=transport).stage(
            release,
            current_version="0.4.3",
            destination=staged,
        )

    assert (staged / "sentinel").read_text(encoding="utf-8") == "previous-valid"


def test_bad_sha256_and_incomplete_download_preserve_previous_staging(
    tmp_path: Path,
) -> None:
    staged = tmp_path / "approved-release"
    staged.mkdir()
    (staged / "sentinel").write_text("previous-valid", encoding="utf-8")
    bundle = _bundle()
    transport, release = _transport_for_bundle(bundle, checksum="0" * 64)

    with pytest.raises(ReleaseError, match="SHA-256"):
        ReleaseStager(transport=transport).stage(
            release, current_version="0.4.3", destination=staged
        )

    assert (staged / "sentinel").exists()

    transport, release = _transport_for_bundle(bundle)
    bundle_url = next(
        url for url in transport.payloads if url.endswith(".tar.gz")
    )
    transport.payloads[bundle_url] = bundle[:-10]
    with pytest.raises(ReleaseError, match="SHA-256"):
        ReleaseStager(transport=transport).stage(
            release, current_version="0.4.3", destination=staged
        )
    assert (staged / "sentinel").exists()


def test_release_with_wrong_asset_names_is_rejected() -> None:
    metadata = _release_metadata()
    metadata["assets"][0]["name"] = "hubinet-ops-latest.tar.gz"
    transport = FakeTransport(metadata, {})

    with pytest.raises(ReleaseError, match="asset"):
        GitHubReleaseDiscovery(transport=transport).check("0.4.3")


def test_valid_bundle_is_verified_and_staged_atomically(tmp_path: Path) -> None:
    staged = tmp_path / "approved-release"
    staged.mkdir()
    (staged / "old").write_text("old", encoding="utf-8")
    bundle = _bundle()
    transport, release = _transport_for_bundle(bundle)

    result = ReleaseStager(transport=transport).stage(
        release, current_version="0.4.3", destination=staged
    )

    assert result["status"] == "staged"
    assert result["version"] == "0.4.4"
    assert result["tag"] == "v0.4.4"
    assert result["commit_sha"] == COMMIT
    assert result["artifact_verification"] == "verified"
    assert (staged / "deploy" / "upgrade-0.4.4-from-pve.sh").is_file()
    assert not (staged / "old").exists()
    assert (tmp_path / "approved-release.previous" / "old").is_file()


def test_file_count_and_unpacked_size_limits_fail_closed(tmp_path: Path) -> None:
    many = _bundle(extra_files=5)
    transport, release = _transport_for_bundle(many)
    with pytest.raises(ReleaseError, match="file count"):
        ReleaseStager(transport=transport, max_files=4).stage(
            release,
            current_version="0.4.3",
            destination=tmp_path / "many",
        )
    large = _bundle()
    transport, release = _transport_for_bundle(large)
    with pytest.raises(ReleaseError, match="unpacked size"):
        ReleaseStager(transport=transport, max_unpacked_bytes=10).stage(
            release,
            current_version="0.4.3",
            destination=tmp_path / "large",
        )


def test_redirect_chain_rejects_non_https_unknown_host_and_excess_hops() -> None:
    valid = [
        "https://github.com/a",
        "https://release-assets.githubusercontent.com/b",
    ]
    validate_redirect_chain(valid)
    with pytest.raises(ReleaseError, match="HTTPS"):
        validate_redirect_chain(["http://github.com/a"])
    with pytest.raises(ReleaseError, match="host"):
        validate_redirect_chain(["https://evil.example/a"])
    with pytest.raises(ReleaseError, match="redirect"):
        validate_redirect_chain(valid * 3)


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (TimeoutError("timed out"), "timed out"),
        (ReleaseError("GitHub API rate limit exceeded"), "rate limit"),
    ],
)
def test_discovery_timeout_and_rate_limit_are_clear(
    error: Exception,
    message: str,
) -> None:
    transport = FakeTransport(_release_metadata(), {})
    transport.error = error
    with pytest.raises(Exception, match=message):
        GitHubReleaseDiscovery(transport=transport).check("0.4.3")


def test_rollout_output_redacts_secret_bearing_lines() -> None:
    output = _safe_output_tail(
        "normal validation line\nAuthorization: Bearer top-secret\n"
        "password=hunter2\nprivate_key=forbidden\nmqtt_password=forbidden-too\n"
    )

    assert "normal validation line" in output
    assert "top-secret" not in output
    assert "hunter2" not in output
    assert "forbidden" not in output
    assert output.count("[redacted sensitive output]") == 4
