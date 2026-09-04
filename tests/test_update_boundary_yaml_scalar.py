"""Direct, hermetic regression for deploy/lib/update-boundaries.sh's
YAML-safe scalar serializer (P2 correction pass).

_update_boundary_activate_config inherits host/user/known_hosts_path from
the installation's OWN existing package_scan.host_control configuration (or
reproduces its documented defaults) -- already-decoded strings this updater
does not choose the shape of. app/inventory_runtime_config.py's own
`_require_text` accepts any non-empty string, including one containing a
literal '"' or a literal '\\'. The activation block used to interpolate
that string directly inside a YAML double-quoted scalar (`"${value}"`),
which a literal '"' breaks (malformed YAML) and a literal '\\' silently
reinterprets (YAML double-quoted scalars process backslash escapes),
changing the decoded value.

This sources only deploy/lib/update-boundaries.sh (never the full
deploy/update-proxmox-0.5.sh entrypoint) and calls exactly one pure,
read-only function (_update_boundary_yaml_dq_scalar): no `pct`, no `ssh`,
no privileged mutation, and no dependency on VMID/UPDATE_RUN_ID or any
other updater state. It needs no Docker sandbox, the same reasoning
tests/test_bootstrap_preflight_dependencies.py already establishes for
sourcing a narrow, read-only slice of a deploy/lib/*.sh file directly.

PyYAML is not installed inside the hardened update-smoke Docker sandbox
image (tests/shell/Dockerfile.bootstrap-smoke installs only pytest), so the
real `yaml.safe_load` round-trip proof belongs HERE, on the ordinary pytest
host where the project's own PyYAML dependency (requirements.txt) is
available -- not inside tests/test_update_proxmox_0_5_smoke.py, which stays
Docker-sandboxed and proves only that the FULL activation flow succeeds and
emits the same exact serialized text (via stdlib `json`, string-level
assertions only, no PyYAML import).
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
UPDATE_BOUNDARIES_SH = REPO_ROOT / "deploy" / "lib" / "update-boundaries.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("python3") is None,
    reason="bash and python3 are required to source the real shell helper",
)


def _yaml_dq_scalar(value: str) -> str:
    """Invoke the real _update_boundary_yaml_dq_scalar shell function."""

    result = subprocess.run(
        ["bash", "-c", 'source "$1" && _update_boundary_yaml_dq_scalar "$2"', "_",
         str(UPDATE_BOUNDARIES_SH), value],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    # Exactly one line: the scalar, nothing else.
    assert result.stdout.endswith("\n")
    return result.stdout[:-1]


@pytest.mark.parametrize(
    "value",
    (
        "plain-host.example.internal",
        '/etc/hubinet-ops/host-control/known"hosts',
        "svc\\deploy",
        'weird\\"mix"\\end',
        "tab\there",
        "unicode-éèhost",
        "",
    ),
    ids=(
        "plain",
        "embedded-double-quote",
        "embedded-backslash",
        "mixed-quote-and-backslash",
        "embedded-tab",
        "non-ascii",
        "empty-string",
    ),
)
def test_yaml_dq_scalar_round_trips_through_real_pyyaml(value: str) -> None:
    scalar = _yaml_dq_scalar(value)
    # It is a legal JSON string literal -- the exact mechanism this helper
    # relies on (YAML 1.2's double-quoted flow scalar syntax is a strict
    # superset of JSON string syntax).
    assert json.loads(scalar) == value
    # And it is accepted by the REAL production YAML loader
    # (app/inventory_runtime_config.py's own `yaml.safe_load`), decoding to
    # the EXACT original string -- not merely "parses", but round-trips
    # byte-for-byte.
    document = f"known_hosts_path: {scalar}\n"
    loaded = yaml.safe_load(document)
    assert loaded == {"known_hosts_path": value}


def test_yaml_dq_scalar_never_breaks_a_surrounding_mapping() -> None:
    """A value crafted to look like it closes the scalar and injects a new
    key must stay a single, inert string value -- never additional YAML
    structure."""

    hostile = '" \nnew_key: injected\nhost_control:\n    known_hosts_path: "'
    scalar = _yaml_dq_scalar(hostile)
    document = f"known_hosts_path: {scalar}\nsibling_key: 1\n"
    loaded = yaml.safe_load(document)
    assert loaded == {"known_hosts_path": hostile, "sibling_key": 1}
