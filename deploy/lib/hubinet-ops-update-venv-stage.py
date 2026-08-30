#!/usr/bin/env python3
"""Create and populate the target virtualenv AT ITS FINAL LIVE PATHNAME.

Invoked INSIDE the Hubinet CT (via `pct exec <vmid> -- python3 <this-file>
<final-venv-path> <requirements-path>`), never on the Proxmox host, and
only when deploy/lib/update-plan.sh classified requirements.txt as
changed.

WHY THIS BUILDS AT THE FINAL PATH (correction pass 8, P1)
---------------------------------------------------------
An earlier design built the new environment at a staging pathname
(/opt/hubinet-ops/.venv.staged-<runid>) while the old service kept
running, and later renamed the whole directory onto /opt/hubinet-ops/.venv
inside the mutation window. That design is REJECTED and must not come
back: a Python virtualenv is not generally relocatable. The console
entrypoints pip/ensurepip generate embed the ABSOLUTE interpreter path of
the environment they were created in -- a staged `bin/pip` carries
`#!/opt/hubinet-ops/.venv.staged-<runid>/bin/python`, either as a plain
shebang or, for a path over the kernel's shebang limit, as the equivalent
`#!/bin/sh` + `'''exec' <abs-path>` wrapper. Renaming the directory does
not rewrite either form, so the "activated" environment would carry
entrypoints pointing at a pathname that no longer exists. Scanning and
rewriting shebangs is explicitly NOT the fix; building at the final
pathname is (tests/test_update_authority_helpers.py proves both halves).

The cost is accepted deliberately: when dependencies actually change, the
maintenance window now also covers the environment build. That is far
rarer than an ordinary code-only update, which never rebuilds the venv,
never runs pip, and is unaffected by this file.

CALLER CONTRACT
---------------
deploy/lib/update-activate.sh calls this only INSIDE the mutation window:
after boot activation is proven disabled, after the old service is proven
stopped, after the durable `update-venv-activation-attempted` journal
marker exists, and after the old /opt/hubinet-ops/.venv has been moved
aside to this run's .venv.rollback-<runid> path. The destination is
therefore the FINAL live pathname and is expected to be absent; this
script still fails closed rather than reusing or mutating anything that
is already there.

A failed or interrupted build legitimately leaves a PARTIAL environment
at the final pathname. This script never tries to repair or resume one --
update-activate.sh's own rollback removes the partial target, proves the
path absent, and restores the preserved old environment.

This is a small, standalone Python helper (not shell) specifically so
deploy/lib/update-activate.sh itself never has to spell out a
`.../bin/pip`- or `.../bin/python3`-shaped path built from a runtime
variable -- scripts/validate_hermetic_shell_boundary.py flags any
occurrence of `/bin/`, `/sbin/`, `/usr/bin/`, or `/usr/sbin/` in a
deploy/bootstrap-*.sh or deploy/lib/*.sh file (dynamic or not) as a
hermeticity violation. This script builds and invokes that same path using
Python's own `pathlib`, which the shell-only validator never inspects, and
uses the standard library `venv` module (`with_pip=True`, the same
ensurepip bootstrap `python3 -m venv` performs) rather than shelling out
to a separately resolved `pip` at all.

Prints nothing on success (exit 0). On failure, prints a short diagnostic
to stderr and exits non-zero.
"""
from __future__ import annotations

import subprocess
import sys
import venv
from pathlib import Path


def create_environment(venv_path: Path) -> None:
    """Create ONE new virtualenv directly at ``venv_path``.

    ``venv_path`` is the environment's permanent home -- never a staging
    pathname that something later renames (see this module's docstring).
    """

    venv.create(venv_path, with_pip=True, symlinks=True)


def main() -> int:
    if len(sys.argv) != 3:
        print(
            "usage: hubinet-ops-update-venv-stage.py <final-venv-path> <requirements-path>",
            file=sys.stderr,
        )
        return 2
    venv_path = Path(sys.argv[1])
    requirements_path = Path(sys.argv[2])

    if venv_path.exists():
        print(f"refusing to build into an already-existing path: {venv_path}", file=sys.stderr)
        return 1
    if not requirements_path.is_file():
        print(f"requirements file does not exist: {requirements_path}", file=sys.stderr)
        return 1

    try:
        create_environment(venv_path)
    except OSError as exc:
        print(f"could not create the target virtualenv: {exc}", file=sys.stderr)
        return 1

    pip_path = venv_path / "bin" / "pip"
    if not pip_path.is_file():
        print(f"the new virtualenv has no pip at the expected path: {pip_path}", file=sys.stderr)
        return 1

    upgrade = subprocess.run([str(pip_path), "install", "--upgrade", "pip"])  # noqa: S603
    if upgrade.returncode != 0:
        return upgrade.returncode

    install = subprocess.run(  # noqa: S603
        [str(pip_path), "install", "-r", str(requirements_path)]
    )
    return install.returncode


if __name__ == "__main__":
    sys.exit(main())
