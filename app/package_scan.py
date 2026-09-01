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
#: A ``Conf`` line's candidate description has the exact same shape as an
#: ``Inst`` line's, minus the installed-version bracket --
#: ``pkgSimulate::RealConfigure`` prints it via the same ``Describe(Pkg,
#: Current=false, Candidate=true)`` helper ``RealInstall`` uses (see
#: ``apt-pkg/algorithms.cc``). A malformed ``Conf`` line -- including the
#: distinct ``"Conf <name:arch> broken"`` shape ``RealConfigure`` prints when
#: ``InstBroken()`` is true, which also registers an ``_error->Error`` in APT
#: itself -- never matches this and fails closed like any other unparseable
#: line.
_CONF_RE = re.compile(
    r"^Conf (?P<name>\S+) \((?P<candidate>\S+) (?P<relstr>[^)]*)\)"
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
#: A dpkg/APT architecture string as ``VerIterator::Arch()`` (or dpkg's own
#: ``Architecture`` status field) ever actually produces: ``all`` for an
#: ``Architecture: all`` package/version, otherwise a real dpkg architecture
#: triplet such as ``amd64``/``i386``/``arm64``.
_ARCHITECTURE_RE = re.compile(r"[a-z][a-z0-9]*(-[a-z0-9]+)*")
_SUMMARY_RE = re.compile(
    r"^(?P<upgraded>\d+) upgraded, (?P<new>\d+) newly installed, "
    r"(?P<removed>\d+) to remove and (?P<held>\d+) not upgraded\.$"
)
#: APT's ``Stats()`` summary (``apt-private/private-output.cc``) prints this
#: additional line, unconditionally right after the ordinary summary, only
#: when dpkg's own broken/not-fully-configured package count
#: (``pkgDepCache::BadCount()``) is nonzero -- pre-existing unfinished dpkg
#: state left over from something else entirely, never attributable to this
#: plan's own approved rows. Seeing this line at all (its count is only ever
#: printed when positive) means the guest's dpkg state cannot safely be
#: reasoned about; see ARCHITECTURE.md, "Binary package identity".
_BAD_COUNT_RE = re.compile(r"^\d+ not fully installed or removed\.$")
_SECURITY_ORIGIN_RE = re.compile(r"(?:^|[/ :])[^ /:]*-security(?:$|[/ :])", re.I)
_BUSY_PATTERNS = (
    "could not get lock",
    "unable to acquire the dpkg frontend lock",
    "is another process using it",
    "could not open lock file",
)


class PackageScanParseError(ValueError):
    """Exact material plan or OS/dpkg evidence was malformed."""


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
    native_architecture: str
    installed_inventory: str
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


def parse_native_architecture(text: str) -> str:
    """Parse the guest's fixed ``dpkg --print-architecture`` output.

    Always a single real dpkg architecture triplet, never ``all`` -- that is
    a version-level flag, never a configured system architecture.
    """

    if not isinstance(text, str) or len(text.encode("utf-8")) > 4 * 1024:
        raise PackageScanParseError("native architecture evidence is missing or oversized")
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) != 1:
        raise PackageScanParseError("native architecture evidence is ambiguous")
    value = lines[0].strip()
    if value == "all" or not _ARCHITECTURE_RE.fullmatch(value):
        raise PackageScanParseError("native architecture evidence is malformed")
    return value


def parse_installed_inventory(text: str) -> dict[tuple[str, str], str]:
    """Parse the guest's fixed, argument-less ``dpkg-query -W`` inventory.

    Independent, read-only proof of every currently *installed* binary
    package's exact ``(name, architecture) -> version``, sourced from
    dpkg's own status database rather than inferred from APT's candidate
    description (see ARCHITECTURE.md, "Binary package identity"). Only rows
    dpkg itself reports as status ``installed`` are kept; every other
    status word (``config-files``, ``half-configured``, ...) is dropped,
    never treated as evidence of a currently installed package.
    """

    if not isinstance(text, str) or len(text.encode("utf-8")) > 16 * 1024 * 1024:
        raise PackageScanParseError(
            "installed package inventory evidence is missing or oversized"
        )
    inventory: dict[tuple[str, str], str] = {}
    for raw_line in text.splitlines():
        if not raw_line:
            continue
        fields = raw_line.split("\t")
        if len(fields) != 4:
            raise PackageScanParseError(
                "installed package inventory contains a malformed row"
            )
        name, architecture, version, status = fields
        if status not in {
            "installed",
            "not-installed",
            "config-files",
            "half-installed",
            "unpacked",
            "half-configured",
            "triggers-awaited",
            "triggers-pending",
        }:
            raise PackageScanParseError(
                "installed package inventory contains an unknown status"
            )
        if status != "installed":
            continue
        if (
            not name
            or not version
            or len(name) > 300
            or len(version) > 500
            or not _ARCHITECTURE_RE.fullmatch(architecture)
        ):
            raise PackageScanParseError(
                "installed package inventory contains a malformed row"
            )
        identity = (name, architecture)
        if identity in inventory:
            raise PackageScanParseError(
                "installed package inventory contains a duplicate (name, architecture)"
            )
        inventory[identity] = version
    return inventory


def _split_qualified_name(raw_name: str) -> tuple[str, str | None]:
    # A colon in the "Inst"/"Conf" name position is exclusively dpkg/APT's
    # architecture-qualifier separator (Debian Policy 5.6.7 forbids ':' in
    # an actual package name), and APT only ever prints it for a
    # foreign-architecture package (never for the native architecture or
    # "all") -- see ARCHITECTURE.md, "Binary package identity".
    if ":" in raw_name:
        name, _, name_architecture = raw_name.partition(":")
        return name, name_architecture
    return raw_name, None


def _parse_candidate_description(relstr: str) -> tuple[str | None, str]:
    """Split one ``(candidate <relstr>)`` tail into ``(origin, architecture)``."""

    relstr_match = _RELSTR_RE.fullmatch(relstr)
    if relstr_match is None:
        raise PackageScanParseError(
            "APT simulation candidate description has no architecture"
        )
    architecture = relstr_match.group("architecture")
    if not _ARCHITECTURE_RE.fullmatch(architecture or ""):
        raise PackageScanParseError("APT simulation candidate architecture is malformed")
    origin = (relstr_match.group("origin") or "").strip() or None
    if origin is not None and len(origin) > 500:
        origin = None
    return origin, architecture


def _resolve_installed_architecture(
    name: str,
    name_architecture: str | None,
    native_architecture: str,
    inventory: Mapping[tuple[str, str], str],
) -> str:
    """Prove exactly one installed ``(name, architecture)`` row for an Inst line.

    Never inferred from the candidate's own architecture bracket. When APT
    printed an explicit ``:arch`` qualifier the row must exist at exactly
    that architecture. Otherwise -- APT prints a bare name for both the
    native architecture and an ``Architecture: all`` package, and dpkg's own
    status records the true installed architecture for either case, which
    can be ``all`` even though the package occupies APT's native package
    slot (see ARCHITECTURE.md) -- exactly one of the native architecture or
    ``all`` must independently match; both matching, or neither, fails
    closed rather than guessing.
    """

    candidates = (
        (name_architecture,)
        if name_architecture is not None
        else (native_architecture, "all")
    )
    matches = [arch for arch in candidates if (name, arch) in inventory]
    if len(matches) != 1:
        raise PackageScanParseError(
            "APT simulation package has no unambiguous independently observed "
            "installed identity"
        )
    return matches[0]


def parse_apt_simulation(
    text: str,
    *,
    native_architecture: str,
    installed_inventory: str,
) -> tuple[PackageScanPackage, ...]:
    """Parse the exact upgrade material plan from stable-English APT output.

    Binary-package identity is proven, never guessed: every ``Inst`` row's
    architecture is the guest's own independently observed dpkg installed
    state (``installed_inventory``, ``native_architecture`` -- see
    ``parse_installed_inventory``/``parse_native_architecture``), not APT's
    candidate description alone. The candidate's own architecture bracket
    and APT's own displayed installed version are cross-checked against
    that independent evidence and must agree exactly, or the plan fails
    closed; a package changing between an architecture-specific binary and
    ``Architecture: all`` is out of this stage's supported scope. Every
    ``Conf`` (configure) action must be exactly bound to one of this same
    approved set of ``Inst`` rows -- a standalone, contradictory, or
    duplicate ``Conf`` fails closed, so a real future workload upgrade can
    never configure a package this exact plan did not already approve.
    """

    if not isinstance(text, str) or len(text.encode("utf-8")) > 8 * 1024 * 1024:
        raise PackageScanParseError("APT simulation output is missing or oversized")
    native = parse_native_architecture(native_architecture)
    inventory = parse_installed_inventory(installed_inventory)

    raw_inst_names: set[str] = set()
    raw_conf_names: set[str] = set()
    changes: list[tuple[str, str, str | None, str, str, str, str | None, bool | None]] = []
    # Each tuple: (raw_name, name, name_architecture, candidate_architecture,
    #              installed_version, candidate_version, origin, security)
    pending_conf: list[tuple[str, str, str]] = []
    # Each tuple: (raw_name, candidate_version, candidate_architecture)
    summary: tuple[int, int, int] | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(("Remv ", "Purg ")):
            raise PackageScanParseError("APT simulation unexpectedly planned a removal")
        if line.startswith("Inst "):
            match = _INST_RE.fullmatch(line)
            if match is None:
                raise PackageScanParseError("APT simulation contains an unparseable change")
            raw_name = match.group("name")
            if raw_name in raw_inst_names:
                raise PackageScanParseError(
                    "APT simulation contains a duplicate (package, architecture)"
                )
            raw_inst_names.add(raw_name)
            name, name_architecture = _split_qualified_name(raw_name)
            origin, candidate_architecture = _parse_candidate_description(
                match.group("relstr")
            )
            if (
                name_architecture is not None
                and name_architecture != candidate_architecture
            ):
                raise PackageScanParseError(
                    "APT simulation package name architecture contradicts "
                    "its candidate architecture"
                )
            security = (
                True
                if origin is not None and _SECURITY_ORIGIN_RE.search(origin)
                else None
            )
            changes.append(
                (
                    raw_name,
                    name,
                    name_architecture,
                    candidate_architecture,
                    match.group("installed"),
                    match.group("candidate"),
                    origin,
                    security,
                )
            )
            continue
        if line.startswith("Conf "):
            match = _CONF_RE.fullmatch(line)
            if match is None:
                raise PackageScanParseError(
                    "APT simulation contains an unparseable configure action"
                )
            raw_name = match.group("name")
            _, conf_architecture = _parse_candidate_description(match.group("relstr"))
            conf_name, conf_name_architecture = _split_qualified_name(raw_name)
            if (
                conf_name_architecture is not None
                and conf_name_architecture != conf_architecture
            ):
                raise PackageScanParseError(
                    "APT simulation configure action architecture is contradictory"
                )
            if raw_name in raw_conf_names:
                raise PackageScanParseError(
                    "APT simulation contains a duplicate configure action"
                )
            raw_conf_names.add(raw_name)
            pending_conf.append(
                (raw_name, match.group("candidate"), conf_architecture)
            )
            continue
        if _BAD_COUNT_RE.fullmatch(line) is not None:
            raise PackageScanParseError(
                "APT simulation reports pre-existing unfinished dpkg state"
            )
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
    if upgraded != len(changes):
        raise PackageScanParseError(
            "APT simulation summary does not match parsed package changes"
        )

    packages: list[PackageScanPackage] = []
    identities: set[tuple[str, str]] = set()
    approved_raw: dict[tuple[str, str], str] = {}
    for (
        raw_name,
        name,
        name_architecture,
        candidate_architecture,
        installed_version,
        candidate_version,
        origin,
        security,
    ) in changes:
        architecture = _resolve_installed_architecture(
            name, name_architecture, native, inventory
        )
        if inventory[(name, architecture)] != installed_version:
            raise PackageScanParseError(
                "APT displayed installed version does not match the "
                "independently observed dpkg installed version"
            )
        if architecture != candidate_architecture:
            raise PackageScanParseError(
                "APT candidate architecture does not match the proven "
                "installed architecture"
            )
        identity = (name, architecture)
        if identity in identities:
            raise PackageScanParseError(
                "APT simulation contains a duplicate (package, architecture)"
            )
        identities.add(identity)
        approved_raw[(raw_name, candidate_version)] = architecture
        packages.append(
            PackageScanPackage(
                package_name=name,
                architecture=architecture,
                installed_version=installed_version,
                candidate_version=candidate_version,
                origin=origin,
                description=None,
                security=security,
            )
        )

    for raw_name, candidate_version, conf_architecture in pending_conf:
        approved_architecture = approved_raw.get((raw_name, candidate_version))
        if approved_architecture is None:
            raise PackageScanParseError(
                "APT simulation configures a package outside the approved plan"
            )
        if conf_architecture != approved_architecture:
            raise PackageScanParseError(
                "APT simulation configure action architecture does not match "
                "its approved package change"
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
        packages = parse_apt_simulation(
            result.simulation_stdout,
            native_architecture=result.native_architecture,
            installed_inventory=result.installed_inventory,
        )
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
