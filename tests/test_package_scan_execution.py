from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType
import uuid

import pytest

from app.inventory import (
    PackageScanFailure,
    PackageScanLifecycle,
    PackageScanRun,
    package_plan_fingerprint,
)
from app.package_scan import (
    HostScanFailure,
    PackageScanParseError,
    classify_command_failure,
    parse_apt_simulation,
    parse_installed_inventory,
    parse_native_architecture,
    parse_os_release,
)
from app.package_scan_host_control import (
    BoundedProcessResult,
    SshPackageScanHostControl,
)


ROOT = Path(__file__).resolve().parents[1]
HELPER_PATH = ROOT / "deploy" / "hubinet-package-scan-helper.py"


def _load_helper() -> ModuleType:
    spec = importlib.util.spec_from_file_location("hubinet_package_scan_helper", HELPER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


helper = _load_helper()


def _inv(*entries: tuple[str, str, str], status: str = "installed") -> str:
    """Build a fixed dpkg-query-shaped installed-inventory fixture.

    ``entries`` are ``(name, architecture, version)`` triples, each an
    independent proof of one currently installed binary package -- the same
    shape ``dpkg-query -W -f='${Package}\\t${Architecture}\\t${Version}\\t
    ${db:Status-Status}\\n'`` produces.
    """

    return "".join(f"{name}\t{arch}\t{version}\t{status}\n" for name, arch, version in entries)


def _sim(
    text: str,
    *,
    native: str = "amd64",
    entries: tuple[tuple[str, str, str], ...] = (),
):
    """``parse_apt_simulation`` with an independently proven installed inventory."""

    return parse_apt_simulation(
        text, native_architecture=native, installed_inventory=_inv(*entries)
    )


DEBIAN_SIMULATION = """\
Reading package lists...
Building dependency tree...
Reading state information...
Calculating upgrade...
Inst openssl [3.0.11-1] (3.0.11-1~deb12u3 Debian-Security:12/oldstable-security [amd64])
Inst apt [2.6.1] (2.6.2 Debian:12/oldstable [amd64])
Conf openssl (3.0.11-1~deb12u3 Debian-Security:12/oldstable-security [amd64])
Conf apt (2.6.2 Debian:12/oldstable [amd64])
2 upgraded, 0 newly installed, 0 to remove and 1 not upgraded.
"""
DEBIAN_INVENTORY = (("openssl", "amd64", "3.0.11-1"), ("apt", "amd64", "2.6.1"))

UBUNTU_SIMULATION = """\
Reading package lists...
Building dependency tree...
Reading state information...
Calculating upgrade...
Inst base-files [13ubuntu10.2] (13ubuntu10.3 Ubuntu:24.04/noble-updates [amd64])
1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.
"""
UBUNTU_INVENTORY = (("base-files", "amd64", "13ubuntu10.2"),)

ZERO_SIMULATION = """\
Reading package lists...
Building dependency tree...
Reading state information...
Calculating upgrade...
0 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.
"""


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        ('ID=debian\nVERSION_ID="12"\n', ("debian", "12")),
        ('NAME="Ubuntu"\nID=ubuntu\nVERSION_ID="24.04"\n', ("ubuntu", "24.04")),
    ),
)
def test_debian_and_ubuntu_os_release_parsing(text: str, expected: tuple[str, str]) -> None:
    assert parse_os_release(text) == expected


def test_unsupported_os_is_classified_without_guessing() -> None:
    with pytest.raises(HostScanFailure) as caught:
        parse_os_release('ID=alpine\nVERSION_ID="3.20"\n')
    assert caught.value.failure_class is PackageScanFailure.UNSUPPORTED_OS


def test_zero_and_multiple_exact_apt_updates() -> None:
    assert _sim(ZERO_SIMULATION) == ()
    debian = _sim(DEBIAN_SIMULATION, entries=DEBIAN_INVENTORY)
    assert tuple(item.package_name for item in debian) == ("apt", "openssl")
    assert debian[1].installed_version == "3.0.11-1"
    assert debian[1].candidate_version == "3.0.11-1~deb12u3"
    assert debian[1].security is True
    assert debian[0].security is None
    assert debian[0].architecture == "amd64"
    assert debian[1].architecture == "amd64"
    ubuntu = _sim(UBUNTU_SIMULATION, entries=UBUNTU_INVENTORY)
    assert ubuntu[0].origin == "Ubuntu:24.04/noble-updates"
    assert ubuntu[0].architecture == "amd64"
    assert ubuntu[0].security is None


@pytest.mark.parametrize(
    (
        "change",
        "installed_entries",
        "expected_name",
        "expected_architecture",
        "expected_installed",
        "expected_candidate",
        "expected_origin",
        "expected_security",
    ),
    (
        (
            "Inst foo [1.0] (1.1 Debian:stable [amd64])",
            (("foo", "amd64", "1.0"),),
            "foo",
            "amd64",
            "1.0",
            "1.1",
            "Debian:stable",
            None,
        ),
        (
            "Inst foo [1.0] (1.1 Debian:stable [amd64]) []",
            (("foo", "amd64", "1.0"),),
            "foo",
            "amd64",
            "1.0",
            "1.1",
            "Debian:stable",
            None,
        ),
        (
            "Inst liblastlog2-2 [2.41-5] "
            "(2.41.5-0+deb13u1 Debian-Security:13/stable-security [amd64]) "
            "[util-linux:amd64 on liblastlog2-2:amd64] [util-linux:amd64 ]",
            (("liblastlog2-2", "amd64", "2.41-5"),),
            "liblastlog2-2",
            "amd64",
            "2.41-5",
            "2.41.5-0+deb13u1",
            "Debian-Security:13/stable-security",
            True,
        ),
        (
            "Inst foo [1.0] (1.1 Debian:stable [amd64]) "
            "[util-linux:amd64 on foo:amd64]",
            (("foo", "amd64", "1.0"),),
            "foo",
            "amd64",
            "1.0",
            "1.1",
            "Debian:stable",
            None,
        ),
        (
            "Inst bind9-host [1:9.20.23-1~deb13u1] "
            "(1:9.20.26-1~deb13u1 Debian-Security:13/stable-security [amd64]) []",
            (("bind9-host", "amd64", "1:9.20.23-1~deb13u1"),),
            "bind9-host",
            "amd64",
            "1:9.20.23-1~deb13u1",
            "1:9.20.26-1~deb13u1",
            "Debian-Security:13/stable-security",
            True,
        ),
        (
            # Architecture: all -- no name qualifier, "all" in the bracket,
            # and dpkg's own installed evidence independently says "all"
            # too (live-confirmed on this repo's own devbox).
            "Inst linux-libc-dev [6.12.101-1] "
            "(6.12.107-1 Debian-Security:13/stable-security [all])",
            (("linux-libc-dev", "all", "6.12.101-1"),),
            "linux-libc-dev",
            "all",
            "6.12.101-1",
            "6.12.107-1",
            "Debian-Security:13/stable-security",
            True,
        ),
        (
            # Foreign architecture -- APT qualifies the name with ':i386'
            # (see ARCHITECTURE.md, "Binary package identity"); the bracket
            # inside the parens still carries the same architecture, and
            # dpkg's own installed evidence must independently agree.
            "Inst libc6:i386 [2.31-13] (2.31-13+deb11u7 Debian:11/stable [i386])",
            (("libc6", "i386", "2.31-13"),),
            "libc6",
            "i386",
            "2.31-13",
            "2.31-13+deb11u7",
            "Debian:11/stable",
            None,
        ),
    ),
)
def test_realistic_apt_shortbreaks_suffix_is_not_material_plan_data(
    change: str,
    installed_entries: tuple[tuple[str, str, str], ...],
    expected_name: str,
    expected_architecture: str,
    expected_installed: str,
    expected_candidate: str,
    expected_origin: str,
    expected_security: bool | None,
) -> None:
    parsed = _sim(
        f"{change}\n1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n",
        entries=installed_entries,
    )
    assert parsed[0].package_name == expected_name
    assert parsed[0].architecture == expected_architecture
    assert parsed[0].installed_version == expected_installed
    assert parsed[0].candidate_version == expected_candidate
    assert parsed[0].origin == expected_origin
    assert parsed[0].security is expected_security


def test_shortbreaks_suffix_does_not_change_material_fingerprint() -> None:
    plain = _sim(
        "Inst foo [1.0] (1.1 Debian:stable [amd64])\n"
        "1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n",
        entries=(("foo", "amd64", "1.0"),),
    )
    shortbreaks = _sim(
        "Inst foo [1.0] (1.1 Debian:stable [amd64]) "
        "[util-linux:amd64 on foo:amd64] [util-linux:amd64 ]\n"
        "1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n",
        entries=(("foo", "amd64", "1.0"),),
    )
    assert plain == shortbreaks
    assert package_plan_fingerprint(plain) == package_plan_fingerprint(shortbreaks)


def test_same_package_name_two_architectures_remain_distinct() -> None:
    # Multiarch regression C/E: foo/amd64 and foo/i386 are two different
    # binary packages, not one row that collapses or overwrites the other.
    sim = (
        "Inst foo [1.0] (1.1 Debian:stable [amd64])\n"
        "Inst foo:i386 [1.0] (1.1 Debian:stable [i386])\n"
        "2 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n"
    )
    parsed = _sim(sim, entries=(("foo", "amd64", "1.0"), ("foo", "i386", "1.0")))
    assert len(parsed) == 2
    identities = {(item.package_name, item.architecture) for item in parsed}
    assert identities == {("foo", "amd64"), ("foo", "i386")}


def test_native_bare_name_resolves_unambiguously_beside_a_foreign_sibling() -> None:
    # Multiarch regression E: a bare (unqualified) Inst name must resolve to
    # the NATIVE-architecture installed row even when a foreign sibling of
    # the same name is also independently installed.
    sim = (
        "Inst foo [1.0] (1.1 Debian:stable [amd64])\n"
        "1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n"
    )
    parsed = _sim(sim, entries=(("foo", "amd64", "1.0"), ("foo", "i386", "1.0")))
    assert parsed == (
        _sim(sim, entries=(("foo", "amd64", "1.0"),))[0],
    )
    assert parsed[0].architecture == "amd64"


def test_architecture_only_difference_changes_the_fingerprint() -> None:
    # Multiarch regression D: same name/installed/candidate, different
    # architecture, must be a different material plan/fingerprint.
    amd64 = _sim(
        "Inst foo [1.0] (1.1 Debian:stable [amd64])\n"
        "1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n",
        entries=(("foo", "amd64", "1.0"),),
    )
    i386 = _sim(
        "Inst foo:i386 [1.0] (1.1 Debian:stable [i386])\n"
        "1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n",
        entries=(("foo", "i386", "1.0"),),
    )
    assert amd64 != i386
    assert package_plan_fingerprint(amd64) != package_plan_fingerprint(i386)


def test_package_row_order_does_not_change_the_fingerprint() -> None:
    # Multiarch regression I: canonical ordering is deterministic, so
    # equality/fingerprinting never depends on host-reported row order.
    entries = (("apt", "amd64", "2.6.1"), ("foo", "i386", "1.0"))
    forward = _sim(
        "Inst apt [2.6.1] (2.6.2 Debian:12/stable [amd64])\n"
        "Inst foo:i386 [1.0] (1.1 Debian:stable [i386])\n"
        "2 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n",
        entries=entries,
    )
    reversed_order = _sim(
        "Inst foo:i386 [1.0] (1.1 Debian:stable [i386])\n"
        "Inst apt [2.6.1] (2.6.2 Debian:12/stable [amd64])\n"
        "2 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n",
        entries=entries,
    )
    assert forward == reversed_order
    assert package_plan_fingerprint(forward) == package_plan_fingerprint(reversed_order)


def test_installed_or_candidate_version_difference_changes_the_fingerprint() -> None:
    # Multiarch regression K/L.
    base = _sim(
        "Inst foo [1.0] (1.1 Debian:stable [amd64])\n"
        "1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n",
        entries=(("foo", "amd64", "1.0"),),
    )
    installed_changed = _sim(
        "Inst foo [1.0.1] (1.1 Debian:stable [amd64])\n"
        "1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n",
        entries=(("foo", "amd64", "1.0.1"),),
    )
    candidate_changed = _sim(
        "Inst foo [1.0] (1.2 Debian:stable [amd64])\n"
        "1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n",
        entries=(("foo", "amd64", "1.0"),),
    )
    assert package_plan_fingerprint(base) != package_plan_fingerprint(installed_changed)
    assert package_plan_fingerprint(base) != package_plan_fingerprint(candidate_changed)


# ---------------------------------------------------------------------------
# P2-1: installed binary-package identity is independently PROVEN, never
# inferred from APT's own candidate architecture bracket alone.
# ---------------------------------------------------------------------------


def test_parse_native_architecture_accepts_one_real_triplet() -> None:
    assert parse_native_architecture("amd64\n") == "amd64"
    assert parse_native_architecture("arm64") == "arm64"


@pytest.mark.parametrize("text", ("all\n", "\n", "amd64\ni386\n", "AMD64\n", "amd 64\n", ""))
def test_parse_native_architecture_fails_closed_on_malformed_or_ambiguous(text: str) -> None:
    with pytest.raises(PackageScanParseError):
        parse_native_architecture(text)


def test_parse_installed_inventory_keeps_only_installed_status_rows() -> None:
    text = (
        "foo\tamd64\t1.0\tinstalled\n"
        "bar\tamd64\t2.0\tconfig-files\n"
        "baz\ti386\t3.0\thalf-configured\n"
    )
    assert parse_installed_inventory(text) == {("foo", "amd64"): "1.0"}


@pytest.mark.parametrize(
    "text",
    (
        "foo\tamd64\t1.0\n",  # missing status field
        "foo\tamd64\t1.0\tunknown-status\n",  # unrecognized status word
        "foo\tAMD64\t1.0\tinstalled\n",  # malformed architecture
        "\tamd64\t1.0\tinstalled\n",  # empty name
        "foo\tamd64\t1.0\tinstalled\nfoo\tamd64\t2.0\tinstalled\n",  # duplicate identity
    ),
)
def test_parse_installed_inventory_fails_closed_on_malformed_rows(text: str) -> None:
    with pytest.raises(PackageScanParseError):
        parse_installed_inventory(text)


def test_installed_architecture_all_from_dpkg_beside_native_apt_bracket_fails_closed() -> None:
    # Regression F/G: installed amd64 -> candidate all (a cross-architecture
    # transition) is out of scope and must fail closed, never silently
    # relabeled from the candidate's own architecture bracket.
    sim = (
        "Inst foo [1.0] (1.1 Debian:stable [all])\n"
        "1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n"
    )
    with pytest.raises(PackageScanParseError):
        _sim(sim, entries=(("foo", "amd64", "1.0"),))


def test_installed_architecture_native_from_dpkg_beside_all_apt_bracket_fails_closed() -> None:
    # Regression G/reverse: installed all -> candidate amd64 also fails closed.
    sim = (
        "Inst foo [1.0] (1.1 Debian:stable [amd64])\n"
        "1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n"
    )
    with pytest.raises(PackageScanParseError):
        _sim(sim, entries=(("foo", "all", "1.0"),))


def test_apt_qualified_name_contradicting_dpkg_architecture_fails_closed() -> None:
    # Regression H: APT says foo:i386 but dpkg's own installed evidence only
    # knows about foo/amd64.
    sim = (
        "Inst foo:i386 [1.0] (1.1 Debian:stable [i386])\n"
        "1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n"
    )
    with pytest.raises(PackageScanParseError):
        _sim(sim, entries=(("foo", "amd64", "1.0"),))


def test_apt_installed_version_disagreeing_with_dpkg_fails_closed() -> None:
    # Regression I / race sanity: APT's own displayed installed version must
    # match dpkg's independently observed installed version exactly.
    sim = (
        "Inst foo [1.0] (1.1 Debian:stable [amd64])\n"
        "1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n"
    )
    with pytest.raises(PackageScanParseError):
        _sim(sim, entries=(("foo", "amd64", "0.9"),))


def test_missing_installed_identity_fails_closed() -> None:
    # Regression J: dpkg has no record at all of this package.
    sim = (
        "Inst foo [1.0] (1.1 Debian:stable [amd64])\n"
        "1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n"
    )
    with pytest.raises(PackageScanParseError):
        _sim(sim, entries=())


def test_ambiguous_bare_name_installed_at_both_native_and_all_fails_closed() -> None:
    # A bare Inst name resolving to BOTH the native architecture and "all"
    # in dpkg's own evidence is a genuine ambiguity, not a case to guess.
    sim = (
        "Inst foo [1.0] (1.1 Debian:stable [amd64])\n"
        "1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n"
    )
    with pytest.raises(PackageScanParseError):
        _sim(sim, entries=(("foo", "amd64", "1.0"), ("foo", "all", "1.0")))


# ---------------------------------------------------------------------------
# P2-2: every `Conf` (configure) action must be exactly bound to an approved
# `Inst` row, and pre-existing unfinished dpkg state fails closed.
# ---------------------------------------------------------------------------


def test_bare_conf_with_matching_inst_architecture_is_accepted() -> None:
    sim = (
        "Inst foo [1.0] (1.1 Debian:stable [amd64])\n"
        "Conf foo (1.1 Debian:stable [amd64])\n"
        "1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n"
    )
    parsed = _sim(sim, entries=(("foo", "amd64", "1.0"),))
    assert tuple(p.package_name for p in parsed) == ("foo",)


def test_two_upgraded_packages_each_configured_is_accepted() -> None:
    sim = (
        "Inst apt [2.6.1] (2.6.2 Debian:12/oldstable [amd64])\n"
        "Inst openssl [3.0.11-1] (3.0.11-1~deb12u3 Debian-Security:12/oldstable-security [amd64])\n"
        "Conf apt (2.6.2 Debian:12/oldstable [amd64])\n"
        "Conf openssl (3.0.11-1~deb12u3 Debian-Security:12/oldstable-security [amd64])\n"
        "2 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n"
    )
    parsed = _sim(sim, entries=DEBIAN_INVENTORY)
    assert len(parsed) == 2


def test_standalone_conf_with_no_approved_inst_fails_closed() -> None:
    # Regression: a package configured but never approved as an Inst change
    # must never silently disappear from the material plan.
    sim = (
        "Inst foo [1.0] (1.1 Debian:stable [amd64])\n"
        "Conf bar (2.0 Debian:stable [amd64])\n"
        "1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n"
    )
    with pytest.raises(PackageScanParseError):
        _sim(sim, entries=(("foo", "amd64", "1.0"),))


def test_conf_candidate_version_mismatch_fails_closed() -> None:
    sim = (
        "Inst foo [1.0] (1.1 Debian:stable [amd64])\n"
        "Conf foo (1.2 Debian:stable [amd64])\n"
        "1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n"
    )
    with pytest.raises(PackageScanParseError):
        _sim(sim, entries=(("foo", "amd64", "1.0"),))


def test_qualified_conf_name_architecture_contradiction_fails_closed() -> None:
    sim = (
        "Inst foo [1.0] (1.1 Debian:stable [amd64])\n"
        "Conf foo:i386 (1.1 Debian:stable [i386])\n"
        "1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n"
    )
    with pytest.raises(PackageScanParseError):
        _sim(sim, entries=(("foo", "amd64", "1.0"),))


def test_bare_conf_architecture_differing_from_bound_inst_fails_closed() -> None:
    sim = (
        "Inst foo [1.0] (1.1 Debian:stable [amd64])\n"
        "Conf foo (1.1 Debian:stable [i386])\n"
        "1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n"
    )
    with pytest.raises(PackageScanParseError):
        _sim(sim, entries=(("foo", "amd64", "1.0"),))


def test_duplicate_conf_for_the_same_action_fails_closed() -> None:
    sim = (
        "Inst foo [1.0] (1.1 Debian:stable [amd64])\n"
        "Conf foo (1.1 Debian:stable [amd64])\n"
        "Conf foo (1.1 Debian:stable [amd64])\n"
        "1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n"
    )
    with pytest.raises(PackageScanParseError):
        _sim(sim, entries=(("foo", "amd64", "1.0"),))


def test_conf_broken_shape_fails_closed() -> None:
    # pkgSimulate::RealConfigure's distinct "Conf <name> broken" shape (and
    # the dependency-listing line that follows it) never matches the normal
    # Conf grammar and must fail closed exactly like any other unparseable
    # line, independent of whatever APT's own returncode does.
    sim = (
        "Inst foo [1.0] (1.1 Debian:stable [amd64])\n"
        "Conf foo:amd64 broken\n"
        " Depends:bar\n"
        "1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n"
    )
    with pytest.raises(PackageScanParseError):
        _sim(sim, entries=(("foo", "amd64", "1.0"),))


def test_zero_change_plan_with_a_standalone_conf_never_becomes_an_empty_match() -> None:
    sim = (
        "Conf bar (2.0 Debian:stable [amd64])\n"
        "0 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n"
    )
    with pytest.raises(PackageScanParseError):
        _sim(sim)


def test_frozen_inst_rows_plus_fresh_extra_standalone_conf_never_matches() -> None:
    # The exact scenario the execution gate cares about: a job's frozen
    # material is one Inst row, but a fresh simulation additionally
    # configures a package the job never approved.
    frozen = _sim(
        "Inst foo [1.0] (1.1 Debian:stable [amd64])\n"
        "1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n",
        entries=(("foo", "amd64", "1.0"),),
    )
    with pytest.raises(PackageScanParseError):
        _sim(
            "Inst foo [1.0] (1.1 Debian:stable [amd64])\n"
            "Conf bar (2.0 Debian:stable [amd64])\n"
            "1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n",
            entries=(("foo", "amd64", "1.0"),),
        )
    assert frozen  # the frozen plan itself parses fine on its own


def test_not_fully_installed_or_removed_fails_closed() -> None:
    sim = (
        "Inst foo [1.0] (1.1 Debian:stable [amd64])\n"
        "1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n"
        "1 not fully installed or removed.\n"
    )
    with pytest.raises(PackageScanParseError):
        _sim(sim, entries=(("foo", "amd64", "1.0"),))


def test_zero_change_plan_with_unfinished_dpkg_state_fails_closed() -> None:
    sim = (
        "0 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n"
        "2 not fully installed or removed.\n"
    )
    with pytest.raises(PackageScanParseError):
        _sim(sim)


def test_purge_action_fails_closed_even_when_summary_claims_no_removals() -> None:
    sim = (
        "Inst foo [1.0] (1.1 Debian:stable [amd64])\n"
        "Purg bar [2.0]\n"
        "1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n"
    )
    with pytest.raises(PackageScanParseError, match="planned a removal"):
        _sim(sim, entries=(("foo", "amd64", "1.0"),))


@pytest.mark.parametrize(
    "text",
    (
        "Inst apt (2.6.2 Debian:12/stable [amd64])\n1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n",
        "Inst apt [2.6.1] broken\n1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n",
        "Inst foo [1.0] (1.1 Debian:stable [amd64]) [unclosed\n1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n",
        "Inst foo [1.0] (1.1 Debian:stable [amd64]) stray\n1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n",
        "Inst apt [2.6.1] (2.6.2 Debian:12/stable [amd64])\n0 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n",
        "Remv obsolete [1.0]\n0 upgraded, 0 newly installed, 1 to remove and 0 not upgraded.\n",
        "Reading package lists...\n",
        # Multiarch regression G: no architecture bracket at all.
        "Inst foo [1.0] (1.1 Debian:stable)\n1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n",
        # Multiarch regression G: an empty architecture bracket.
        "Inst foo [1.0] (1.1 Debian:stable [])\n1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n",
        # Multiarch regression F: the name's ':arch' qualifier contradicts
        # the candidate description's own architecture bracket.
        "Inst foo:i386 [1.0] (1.1 Debian:stable [amd64])\n1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n",
        # Multiarch regression H: duplicate (package, architecture).
        (
            "Inst foo [1.0] (1.1 Debian:stable [amd64])\n"
            "Inst foo [1.0] (1.1 Debian:stable [amd64])\n"
            "2 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n"
        ),
    ),
)
def test_malformed_or_inexact_simulation_fails_scan(text: str) -> None:
    with pytest.raises(PackageScanParseError):
        _sim(text, entries=(("foo", "amd64", "1.0"), ("apt", "amd64", "2.6.1")))


def test_package_manager_busy_has_distinct_classification() -> None:
    assert classify_command_failure(
        stage="metadata_refresh",
        returncode=100,
        stderr="E: Could not get lock /var/lib/dpkg/lock-frontend",
    ) is PackageScanFailure.PACKAGE_MANAGER_BUSY
    assert classify_command_failure(
        stage="metadata_refresh", returncode=100, stderr="repository unavailable"
    ) is PackageScanFailure.METADATA_REFRESH_FAILED


def _request(*, vmid=101, operation="scan_packages", expected_node="pve-a"):
    return {
        "request_version": 1,
        "operation": operation,
        "target": {"vmid": vmid, "expected_node": expected_node},
        "context": {
            "scan_run_id": str(uuid.uuid4()),
            "resource_id": str(uuid.uuid4()),
            "binding_id": str(uuid.uuid4()),
            "locator_generation": 2,
            "resource_continuity_revision": 3,
        },
    }


class FakeHelperRunner:
    def __init__(
        self,
        *,
        resource_type: str = "lxc",
        status: str = "running",
        node: str = "pve-a",
        local_node: str = "pve-a",
        migrate_after_checks: int | None = None,
        migrate_to_node: str = "pve-c",
        os_release: str = 'ID=debian\nVERSION_ID="12"\n',
        apt_version: str = "apt 2.6.1 (amd64)\n",
        apt_version_returncode: int = 0,
        native_architecture: str = "amd64\n",
        installed_inventory: str = "",
        update_returncode: int = 0,
        update_stderr: str = "",
        simulation_returncode: int = 0,
        simulation_stdout: str = ZERO_SIMULATION,
        simulation_stderr: str = "",
        remote_returncode: int | None = None,
        remote_stderr: str = "",
        remote_failure_command: str | None = None,
        os_returncode: int = 0,
        timed_out_command: str | None = None,
    ) -> None:
        self.resource_type = resource_type
        self.status = status
        self.node = node
        self.local_node = local_node
        self.migrate_after_checks = migrate_after_checks
        self.migrate_to_node = migrate_to_node
        self.os_release = os_release
        self.apt_version = apt_version
        self.apt_version_returncode = apt_version_returncode
        self.native_architecture = native_architecture
        self.installed_inventory = installed_inventory
        self.update_returncode = update_returncode
        self.update_stderr = update_stderr
        self.simulation_returncode = simulation_returncode
        self.simulation_stdout = simulation_stdout
        self.simulation_stderr = simulation_stderr
        self.remote_returncode = remote_returncode
        self.remote_stderr = remote_stderr
        self.remote_failure_command = remote_failure_command
        self.os_returncode = os_returncode
        self.timed_out_command = timed_out_command
        self.calls: list[tuple[str, ...]] = []
        self._target_checks = 0

    def __call__(self, argv, _timeout, _max_output):
        self.calls.append(argv)
        rendered = " ".join(argv)
        if self.timed_out_command and self.timed_out_command in rendered:
            return helper.CommandResult(-9, b"", b"", timed_out=True)
        if argv[0] == "pvesh" and argv[2] == "/cluster/status":
            rows = [
                {
                    "type": "node",
                    "name": self.local_node,
                    "local": 1,
                    "nodeid": 0,
                    "online": 1,
                }
            ]
            return helper.CommandResult(0, json.dumps(rows).encode(), b"")
        if argv[0] == "pvesh" and argv[2] == "/cluster/resources":
            self._target_checks += 1
            current_node = self.node
            if (
                self.migrate_after_checks is not None
                and self._target_checks > self.migrate_after_checks
            ):
                current_node = self.migrate_to_node
            rows = [
                {
                    "vmid": 101,
                    "type": self.resource_type,
                    "node": current_node,
                    "status": self.status,
                }
            ]
            return helper.CommandResult(0, json.dumps(rows).encode(), b"")
        if (
            self.remote_returncode is not None
            and argv[0] == "ssh"
            and (
                self.remote_failure_command is None
                or self.remote_failure_command in rendered
            )
        ):
            return helper.CommandResult(
                self.remote_returncode, b"", self.remote_stderr.encode()
            )
        if "/etc/os-release" in rendered:
            return helper.CommandResult(self.os_returncode, self.os_release.encode(), b"")
        if "apt-get --version" in rendered:
            return helper.CommandResult(
                self.apt_version_returncode, self.apt_version.encode(), b""
            )
        if "update" in rendered and "apt-get" in rendered:
            return helper.CommandResult(
                self.update_returncode, b"", self.update_stderr.encode()
            )
        if "upgrade" in rendered:
            return helper.CommandResult(
                self.simulation_returncode,
                self.simulation_stdout.encode(),
                self.simulation_stderr.encode(),
            )
        if "--print-architecture" in rendered:
            return helper.CommandResult(0, self.native_architecture.encode(), b"")
        if "dpkg-query" in rendered:
            return helper.CommandResult(0, self.installed_inventory.encode(), b"")
        if "/var/run/reboot-required" in rendered:
            return helper.CommandResult(1, b"", b"")
        raise AssertionError(f"unexpected command shape: {argv!r}")


def test_helper_accepts_only_typed_operation_and_strict_vmid() -> None:
    with pytest.raises(helper.RequestError, match="unknown"):
        helper.validate_request(_request(operation="shell"))
    for malformed in ("101", 0, -1, True, 99, 1_000_000_000):
        with pytest.raises(helper.RequestError, match="vmid"):
            helper.validate_request(_request(vmid=malformed))
    request = _request()
    request["command"] = "apt-get upgrade"
    with pytest.raises(helper.RequestError, match="exact"):
        helper.validate_request(request)


def test_helper_success_uses_only_fixed_pvesh_and_pct_shapes() -> None:
    runner = FakeHelperRunner()
    response = helper.handle_request(_request(), runner=runner)
    assert response["ok"] is True
    assert response["reboot_required"] is None
    assert response["native_architecture"] == "amd64\n"
    assert response["installed_inventory"] == ""
    assert all(call[0] in {"pvesh", "pct"} for call in runner.calls)
    assert any(call[-2:] == ("apt-get", "--version") for call in runner.calls)
    assert any(
        call[-4:] == ("apt-get", "update", "-qq", "--error-on=any")
        for call in runner.calls
    )
    assert any(call[-3:] == ("apt-get", "-s", "upgrade") for call in runner.calls)
    assert any(call[-2:] == ("dpkg", "--print-architecture") for call in runner.calls)
    assert any(call[-3] == "dpkg-query" for call in runner.calls)
    assert not any("eval" in argument for call in runner.calls for argument in call)


@pytest.mark.parametrize(
    ("apt_version", "supported"),
    (
        ("apt 2.1.15 (amd64)\n", False),
        ("apt 2.1.16 (amd64)\n", True),
        ("apt 2.6.1 (amd64)\n", True),
        ("apt 3.0.3 (amd64)\n", True),
    ),
)
def test_helper_enforces_strict_metadata_refresh_apt_version_floor(
    apt_version: str, supported: bool
) -> None:
    runner = FakeHelperRunner(apt_version=apt_version)
    response = helper.handle_request(_request(), runner=runner)
    update_calls = [
        call for call in runner.calls if "apt-get" in call and "update" in call
    ]
    simulation_calls = [
        call for call in runner.calls if call[-3:] == ("apt-get", "-s", "upgrade")
    ]
    assert response["ok"] is supported
    assert bool(update_calls) is supported
    assert bool(simulation_calls) is supported
    if supported:
        assert len(update_calls) == 1
        assert update_calls[0][-4:] == (
            "apt-get", "update", "-qq", "--error-on=any"
        )
    if not supported:
        assert response["error"] == {
            "classification": "unsupported_os",
            "message": "guest APT version does not support strict metadata refresh",
        }


def test_helper_rejects_ubuntu_apt_2_0_without_legacy_fallback() -> None:
    runner = FakeHelperRunner(
        os_release='NAME="Ubuntu"\nID=ubuntu\nVERSION_ID="20.04"\n',
        apt_version="apt 2.0.10 (amd64)\n",
    )
    response = helper.handle_request(_request(), runner=runner)
    apt_calls = [call for call in runner.calls if "apt-get" in call]
    assert response["ok"] is False
    assert response["os"] == {"id": "ubuntu", "version": "20.04"}
    assert response["error"]["classification"] == "unsupported_os"
    assert len(apt_calls) == 1
    assert apt_calls[0][-2:] == ("apt-get", "--version")


@pytest.mark.parametrize(
    "runner",
    (
        FakeHelperRunner(apt_version_returncode=1),
        FakeHelperRunner(apt_version="apt version unknown\n"),
        FakeHelperRunner(apt_version="apt 2.1 (amd64)\n"),
    ),
)
def test_helper_unproven_apt_capability_fails_closed(runner) -> None:
    response = helper.handle_request(_request(), runner=runner)
    assert response["ok"] is False
    assert response["error"]["classification"] == "execution_failed"
    assert not any("update" in call for call in runner.calls)
    assert not any("upgrade" in call for call in runner.calls)


@pytest.mark.parametrize(
    ("runner", "classification"),
    (
        (FakeHelperRunner(status="stopped"), "guest_unavailable"),
        (FakeHelperRunner(resource_type="qemu"), "unsupported_resource_type"),
        (FakeHelperRunner(node="pve-b"), "stale_target"),
        (FakeHelperRunner(os_release='ID=alpine\nVERSION_ID="3.20"\n'), "unsupported_os"),
        (
            FakeHelperRunner(update_returncode=100, update_stderr="repository unavailable"),
            "metadata_refresh_failed",
        ),
        (
            FakeHelperRunner(
                update_returncode=100,
                update_stderr="E: Could not get lock /var/lib/dpkg/lock-frontend",
            ),
            "package_manager_busy",
        ),
        (
            FakeHelperRunner(simulation_returncode=100, simulation_stderr="failed"),
            "simulation_failed",
        ),
        (FakeHelperRunner(timed_out_command="apt-get update"), "timeout"),
    ),
)
def test_helper_classifies_ordinary_failures(runner, classification: str) -> None:
    response = helper.handle_request(_request(), runner=runner)
    assert response["ok"] is False
    assert response["error"]["classification"] == classification
    assert "stdout" not in response["error"]
    assert "stderr" not in response["error"]
    if classification in {"metadata_refresh_failed", "package_manager_busy"}:
        # Corrective pass, Finding 1: a partial/failing metadata refresh
        # must never proceed to the upgrade simulation -- the resulting
        # scan must be a failure, never a successful (and possibly stale)
        # "0 updates" plan.
        assert not any("upgrade" in " ".join(call) for call in runner.calls)


def test_helper_metadata_refresh_uses_fail_on_any_error_and_never_simulates_on_failure() -> None:
    # Corrective pass, Finding 1 witness: prove (1) the actual fixed argv
    # sent to the guest carries APT's own fail-on-any-error option, and
    # (2) a refresh APT reports as failed because of it never reaches the
    # simulation stage, so the scan is a hard failure rather than a
    # successful exact plan against stale/incomplete indexes.
    runner = FakeHelperRunner(
        update_returncode=100,
        update_stderr="E: Some index files failed to download",
    )
    response = helper.handle_request(_request(), runner=runner)
    update_calls = [
        call for call in runner.calls if call[0] == "pct" and "update" in call and "apt-get" in call
    ]
    assert len(update_calls) == 1, "expected exactly one fixed apt-get update argv"
    assert update_calls[0][-4:] == ("apt-get", "update", "-qq", "--error-on=any")
    assert not any("upgrade" in call for call in runner.calls)
    assert response["ok"] is False
    assert response["error"]["classification"] == "metadata_refresh_failed"


def test_helper_runs_local_node_lxc_directly_without_ssh() -> None:
    runner = FakeHelperRunner(node="pve-a", local_node="pve-a")
    response = helper.handle_request(_request(expected_node="pve-a"), runner=runner)
    assert response["ok"] is True
    assert not any(call[0] == "ssh" for call in runner.calls)
    assert any(call[0] == "pct" and call[1] == "exec" for call in runner.calls)


def test_helper_routes_remote_node_lxc_execution_over_cluster_ssh() -> None:
    # The bootstrap/entry PVE node is pve-a, but the LXC's expected (and
    # cluster-resources-confirmed) node is pve-b. Every fixed pct exec shape
    # must be routed to pve-b rather than run locally on pve-a.
    runner = FakeHelperRunner(node="pve-b", local_node="pve-a")
    response = helper.handle_request(_request(expected_node="pve-b"), runner=runner)
    assert response["ok"] is True
    assert not any(call[0] == "pct" for call in runner.calls)
    ssh_calls = [call for call in runner.calls if call[0] == "ssh"]
    # os-release, apt-version, apt-get update, apt-get -s upgrade,
    # dpkg --print-architecture, dpkg-query, reboot-required.
    assert len(ssh_calls) == 7
    for call in ssh_calls:
        assert call[-2] == "root@pve-b"
        assert "BatchMode=yes" in call
        assert "StrictHostKeyChecking=yes" in call
        remote_command = call[-1]
        assert remote_command.startswith("pct exec 101 --")
        assert "eval" not in remote_command
    assert any("apt-get update -qq" in call[-1] for call in ssh_calls)
    assert any("apt-get -s upgrade" in call[-1] for call in ssh_calls)
    assert any("dpkg --print-architecture" in call[-1] for call in ssh_calls)
    assert any("dpkg-query -W" in call[-1] for call in ssh_calls)


def test_helper_migration_between_validations_fails_closed_never_success() -> None:
    # The guest starts on the expected node but migrates to a third node
    # partway through the fixed operation sequence. The stale-target check
    # that precedes every guest operation must catch this and stop the scan
    # rather than let a later step commit success against the wrong node.
    runner = FakeHelperRunner(
        node="pve-b",
        local_node="pve-a",
        migrate_after_checks=1,
        migrate_to_node="pve-c",
    )
    response = helper.handle_request(_request(expected_node="pve-b"), runner=runner)
    assert response["ok"] is False
    assert response["error"]["classification"] == "stale_target"
    # Only the first (os-release) guest command should have been attempted.
    assert sum(1 for call in runner.calls if call[0] == "ssh") == 1


@pytest.mark.parametrize(
    "failed_command",
    ("/etc/os-release", "apt-get --version", "apt-get update -qq", "apt-get -s upgrade"),
)
def test_helper_remote_node_transport_failure_is_execution_failed(
    failed_command: str,
) -> None:
    runner = FakeHelperRunner(
        node="pve-b",
        local_node="pve-a",
        remote_returncode=255,
        remote_stderr="ssh: connect to host pve-b port 22: Connection refused",
        remote_failure_command=failed_command,
    )
    response = helper.handle_request(_request(expected_node="pve-b"), runner=runner)
    assert response["ok"] is False
    assert response["error"]["classification"] == "execution_failed"


def test_helper_failed_os_release_retrieval_is_not_unsupported_os() -> None:
    response = helper.handle_request(_request(), runner=FakeHelperRunner(os_returncode=1))
    assert response["ok"] is False
    assert response["error"]["classification"] == "guest_unavailable"


def test_helper_rejects_expected_node_with_shell_metacharacters() -> None:
    for hostile in (
        "pve-a; rm -rf /",
        "pve-a && evil",
        "$(evil)",
        "-oProxyCommand=evil",
        "pve a",
        "pve-a\nrm -rf /",
    ):
        with pytest.raises(helper.RequestError, match="expected_node"):
            helper.validate_request(_request(expected_node=hostile))


def _scan_run() -> PackageScanRun:
    return PackageScanRun(
        scan_run_id=str(uuid.uuid4()),
        resource_id=str(uuid.uuid4()),
        inventory_source_id=str(uuid.uuid4()),
        committed_source_config_revision=4,
        committed_endpoint_id=str(uuid.uuid4()),
        committed_canonical_transport_locator="https://pve.example:8006",
        committed_canonicalization_contract_version=1,
        committed_transport_trust_revision=2,
        provider_contract_version=1,
        attempt_sequence=1,
        expected_binding_id=str(uuid.uuid4()),
        expected_locator_generation=2,
        expected_resource_continuity_revision=3,
        expected_vmid=101,
        expected_node_id=str(uuid.uuid4()),
        expected_node_name="pve-a",
        started_at="2026-08-28T12:00:00+00:00",
        lifecycle=PackageScanLifecycle.RUNNING,
        completed_at=None,
        outcome=None,
        failure_class=None,
        error_message=None,
        os_id=None,
        os_version=None,
        pending_count=None,
        plan_fingerprint=None,
        reboot_required=None,
    )


def test_ssh_host_control_pins_key_and_host_key_and_sends_json_on_stdin(tmp_path: Path) -> None:
    run = _scan_run()
    captured = {}

    def runner(argv, input_bytes, timeout, max_output):
        captured.update(
            argv=argv, input_bytes=input_bytes, timeout=timeout, max_output=max_output
        )
        request = json.loads(input_bytes)
        response = {
            "response_version": 1,
            "ok": True,
            "context": request["context"],
            "os_release": 'ID=debian\nVERSION_ID="12"\n',
            "native_architecture": "amd64\n",
            "installed_inventory": "",
            "simulation": {"returncode": 0, "stdout": ZERO_SIMULATION},
            "reboot_required": None,
        }
        return BoundedProcessResult(0, json.dumps(response).encode(), b"")

    client = SshPackageScanHostControl(
        host="192.0.2.10",
        port=22,
        user="hubinet-scan",
        private_key_path=tmp_path.resolve() / "id_ed25519",
        known_hosts_path=tmp_path.resolve() / "known_hosts",
        timeout_seconds=900,
        max_result_bytes=8 * 1024 * 1024,
        runner=runner,
    )
    result = client.scan_packages(run)
    assert result.simulation_stdout == ZERO_SIMULATION
    assert result.native_architecture == "amd64\n"
    assert result.installed_inventory == ""
    argv = captured["argv"]
    assert "StrictHostKeyChecking=yes" in argv
    assert any(str(item).startswith("UserKnownHostsFile=") for item in argv)
    assert "ClearAllForwardings=yes" in argv
    assert argv[-1] == "hubinet-scan@192.0.2.10"
    assert json.loads(captured["input_bytes"])["operation"] == "scan_packages"


@pytest.mark.parametrize(
    "result, expected",
    (
        (BoundedProcessResult(-9, b"", b"", timed_out=True), TimeoutError),
        (
            BoundedProcessResult(-9, b"", b"", output_exceeded=True),
            HostScanFailure,
        ),
        (BoundedProcessResult(255, b"", b"ssh failed"), HostScanFailure),
    ),
)
def test_ssh_host_control_classifies_transport_bounds(
    tmp_path: Path, result: BoundedProcessResult, expected: type[Exception]
) -> None:
    client = SshPackageScanHostControl(
        host="pve-a",
        port=22,
        user="hubinet-scan",
        private_key_path=tmp_path.resolve() / "id_ed25519",
        known_hosts_path=tmp_path.resolve() / "known_hosts",
        timeout_seconds=900,
        max_result_bytes=8 * 1024 * 1024,
        runner=lambda *_args: result,
    )
    with pytest.raises(expected):
        client.scan_packages(_scan_run())
