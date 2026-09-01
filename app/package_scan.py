"""Debian/Ubuntu LXC package scan parsing and orchestration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re
import shlex
from typing import Any, Protocol

from app.inventory import (
    InventoryAuthority,
    PackageScanFailure,
    PackageScanOutcome,
    PackageScanPackage,
    PackageScanRun,
)


_INST_RE = re.compile(
    r"^Inst (?P<name>\S+) \[(?P<installed>[^\]\s]+)\] "
    r"\((?P<candidate>\S+) (?P<relstr>[^)]*)\)"
    r"(?: \[[^\[\]\r\n]*\])*$"
)
#: ``apt-get -s upgrade``'s candidate description is always
#: ``"<label list> [<architecture>]"`` or, when there is no label list,
#: ``" [<architecture>]"`` -- ``pkgCache::VerIterator::RelStr()``
#: unconditionally appends `` [<Arch()>]`` after any release-label text (see
#: ARCHITECTURE.md, "Binary package identity", and the PR body's cited
#: research). The architecture bracket is therefore always the last bracket
#: group in the candidate description, never absent, and never optional
#: metadata; a non-greedy origin group anchored by ``fullmatch`` finds it
#: even if release-label text itself happened to contain other brackets.
_RELSTR_RE = re.compile(r"(?:(?P<origin>.*?) )?\[(?P<architecture>[^\[\]]*)\]")
#: A dpkg/APT architecture string as ``VerIterator::Arch()`` ever actually
#: produces: ``all`` for an ``Architecture: all`` version, otherwise a real
#: dpkg architecture triplet such as ``amd64``/``i386``/``arm64``.
_ARCHITECTURE_RE = re.compile(r"[a-z][a-z0-9]*(-[a-z0-9]+)*")
_SUMMARY_RE = re.compile(
    r"^(?P<upgraded>\d+) upgraded, (?P<new>\d+) newly installed, "
    r"(?P<removed>\d+) to remove and (?P<held>\d+) not upgraded\.$"
)
_SECURITY_ORIGIN_RE = re.compile(r"(?:^|[/ :])[^ /:]*-security(?:$|[/ :])", re.I)
_BUSY_PATTERNS = (
    "could not get lock",
    "unable to acquire the dpkg frontend lock",
    "is another process using it",
    "could not open lock file",
)


class PackageScanParseError(ValueError):
    """Exact material plan or OS evidence was malformed."""


@dataclass(frozen=True, slots=True)
class HostScanFailure(Exception):
    failure_class: PackageScanFailure
    message: str
    os_id: str | None = None
    os_version: str | None = None

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True, slots=True)
class HostScanResult:
    context: Mapping[str, Any]
    os_release: str
    simulation_stdout: str
    reboot_required: bool | None


class PackageScanHostControl(Protocol):
    def scan_packages(self, run: PackageScanRun) -> HostScanResult:
        """Execute the one allowed typed host-control operation."""


def parse_os_release(text: str) -> tuple[str, str]:
    if not isinstance(text, str) or len(text.encode("utf-8")) > 64 * 1024:
        raise PackageScanParseError("OS release evidence is missing or oversized")
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise PackageScanParseError("OS release evidence contains a malformed line")
        key, raw_value = line.split("=", 1)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in values:
            raise PackageScanParseError("OS release evidence contains an invalid field")
        try:
            parsed = shlex.split(raw_value, posix=True)
        except ValueError as exc:
            raise PackageScanParseError("OS release evidence contains invalid quoting") from exc
        if len(parsed) > 1:
            raise PackageScanParseError("OS release evidence contains an ambiguous value")
        values[key] = parsed[0] if parsed else ""
    os_id = values.get("ID", "").lower()
    version = values.get("VERSION_ID") or values.get("VERSION_CODENAME") or ""
    if os_id not in {"debian", "ubuntu"}:
        raise HostScanFailure(
            PackageScanFailure.UNSUPPORTED_OS,
            "guest operating system is not supported for package scanning",
            os_id or None,
            version or None,
        )
    if not version or len(os_id) > 100 or len(version) > 200:
        raise PackageScanParseError("OS release evidence has no bounded version")
    return os_id, version


def parse_apt_simulation(text: str) -> tuple[PackageScanPackage, ...]:
    """Parse the exact upgrade material plan from stable-English APT output."""

    if not isinstance(text, str) or len(text.encode("utf-8")) > 8 * 1024 * 1024:
        raise PackageScanParseError("APT simulation output is missing or oversized")
    packages: list[PackageScanPackage] = []
    identities: set[tuple[str, str]] = set()
    summary: tuple[int, int, int] | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("Remv "):
            raise PackageScanParseError("APT simulation unexpectedly planned a removal")
        if line.startswith("Inst "):
            match = _INST_RE.fullmatch(line)
            if match is None:
                raise PackageScanParseError("APT simulation contains an unparseable change")
            raw_name = match.group("name")
            # A colon in the "Inst" name position is exclusively dpkg/APT's
            # architecture-qualifier separator (Debian Policy 5.6.7 forbids
            # ':' in an actual package name), and APT only ever prints it for
            # a foreign-architecture package (never for the native
            # architecture or "all") -- see ARCHITECTURE.md, "Binary package
            # identity". When present it must agree with the architecture
            # this line's candidate description carries; it is never trusted
            # alone, because it is silently absent for the common native case.
            if ":" in raw_name:
                name, _, name_architecture = raw_name.partition(":")
            else:
                name, name_architecture = raw_name, None

            relstr_match = _RELSTR_RE.fullmatch(match.group("relstr"))
            if relstr_match is None:
                raise PackageScanParseError(
                    "APT simulation candidate description has no architecture"
                )
            architecture = relstr_match.group("architecture")
            if not _ARCHITECTURE_RE.fullmatch(architecture or ""):
                raise PackageScanParseError(
                    "APT simulation candidate architecture is malformed"
                )
            if name_architecture is not None and name_architecture != architecture:
                raise PackageScanParseError(
                    "APT simulation package name architecture contradicts "
                    "its candidate architecture"
                )

            identity = (name, architecture)
            if identity in identities:
                raise PackageScanParseError(
                    "APT simulation contains a duplicate (package, architecture)"
                )
            identities.add(identity)
            origin = (relstr_match.group("origin") or "").strip() or None
            if origin is not None and len(origin) > 500:
                origin = None
            packages.append(
                PackageScanPackage(
                    package_name=name,
                    architecture=architecture,
                    installed_version=match.group("installed"),
                    candidate_version=match.group("candidate"),
                    origin=origin,
                    description=None,
                    security=(
                        True
                        if origin is not None and _SECURITY_ORIGIN_RE.search(origin)
                        else None
                    ),
                )
            )
            continue
        matched_summary = _SUMMARY_RE.fullmatch(line)
        if matched_summary is not None:
            if summary is not None:
                raise PackageScanParseError("APT simulation contains duplicate summaries")
            summary = (
                int(matched_summary.group("upgraded")),
                int(matched_summary.group("new")),
                int(matched_summary.group("removed")),
            )
    if summary is None:
        raise PackageScanParseError("APT simulation has no exact plan summary")
    upgraded, newly_installed, removed = summary
    if newly_installed != 0 or removed != 0:
        raise PackageScanParseError(
            "APT simulation includes a change without an installed package version"
        )
    if upgraded != len(packages):
        raise PackageScanParseError(
            "APT simulation summary does not match parsed package changes"
        )
    return tuple(
        sorted(packages, key=lambda package: (package.package_name, package.architecture))
    )


def classify_command_failure(
    *, stage: str, returncode: int, stderr: str
) -> PackageScanFailure:
    combined = stderr.lower()
    if any(pattern in combined for pattern in _BUSY_PATTERNS):
        return PackageScanFailure.PACKAGE_MANAGER_BUSY
    if stage == "metadata_refresh":
        return PackageScanFailure.METADATA_REFRESH_FAILED
    if stage == "simulation":
        return PackageScanFailure.SIMULATION_FAILED
    return PackageScanFailure.EXECUTION_FAILED


def expected_host_context(run: PackageScanRun) -> dict[str, Any]:
    return {
        "scan_run_id": run.scan_run_id,
        "resource_id": run.resource_id,
        "binding_id": run.expected_binding_id,
        "locator_generation": run.expected_locator_generation,
        "resource_continuity_revision": run.expected_resource_continuity_revision,
    }


def run_package_scan(
    authority: InventoryAuthority,
    run: PackageScanRun,
    host_control: PackageScanHostControl,
) -> PackageScanRun:
    """Execute and durably finish one already-issued scan attempt."""

    if not authority.package_scan_context_is_current(run.scan_run_id):
        return authority.finalize_failed_package_scan(
            run.scan_run_id,
            failure_class=PackageScanFailure.STALE_TARGET,
            error_message="resource context changed before package scan execution",
        )
    try:
        result = host_control.scan_packages(run)
        if dict(result.context) != expected_host_context(run):
            raise HostScanFailure(
                PackageScanFailure.STALE_TARGET,
                "host-control response context does not match the scan request",
            )
        os_id, os_version = parse_os_release(result.os_release)
        packages = parse_apt_simulation(result.simulation_stdout)
        return authority.finalize_successful_package_scan(
            run.scan_run_id,
            os_id=os_id,
            os_version=os_version,
            packages=packages,
            reboot_required=result.reboot_required,
        )
    except HostScanFailure as exc:
        return authority.finalize_failed_package_scan(
            run.scan_run_id,
            failure_class=exc.failure_class,
            error_message=exc.message[:500],
            os_id=exc.os_id,
            os_version=exc.os_version,
        )
    except PackageScanParseError as exc:
        return authority.finalize_failed_package_scan(
            run.scan_run_id,
            failure_class=PackageScanFailure.MALFORMED_PLAN,
            error_message=str(exc)[:500],
        )
    except TimeoutError:
        return authority.finalize_failed_package_scan(
            run.scan_run_id,
            failure_class=PackageScanFailure.TIMEOUT,
            error_message="package scan host-control request timed out",
        )
    except Exception:  # noqa: BLE001 - classify without publishing raw exception detail
        return authority.finalize_failed_package_scan(
            run.scan_run_id,
            failure_class=PackageScanFailure.EXECUTION_FAILED,
            error_message="package scan host-control execution failed",
        )
