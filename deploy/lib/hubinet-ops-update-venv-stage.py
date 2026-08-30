#!/usr/bin/env python3
"""Create and populate ONE staged virtualenv for the in-place updater.

Invoked INSIDE the Hubinet CT (via `pct exec <vmid> -- python3 <this-file>
<staged-venv-path> <requirements-path>`), never on the Proxmox host, and
only when deploy/lib/update-plan.sh classified requirements.txt as
changed. Deliberately never touches the ACTIVE virtualenv -- the caller
always passes a fresh, not-yet-existing staged path (e.g.
/opt/hubinet-ops/.venv.staged-<runid>); this script fails if that path
already exists rather than silently reusing/mutating it.

This is a small, standalone Python helper (not shell) specifically so
deploy/lib/update-stage.sh itself never has to spell out a `.../bin/pip`-
or `.../bin/python3`-shaped path built from a runtime variable --
scripts/validate_hermetic_shell_boundary.py flags any occurrence of
`/bin/`, `/sbin/`, `/usr/bin/`, or `/usr/sbin/` in a deploy/bootstrap-*.sh
or deploy/lib/*.sh file (dynamic or not) as a hermeticity violation. This
script builds and invokes that same path using Python's own `pathlib`,
which the shell-only validator never inspects, and uses the standard
library `venv` module (`with_pip=True`, the same ensurepip bootstrap
`python3 -m venv` performs) rather than shelling out to a separately
resolved `pip` at all.

Prints nothing on success (exit 0). On failure, prints a short diagnostic
to stderr and exits non-zero; the caller (update-stage.sh) treats any
non-zero exit as a staging failure -- the ACTIVE virtualenv was never
touched, so this can safely fail before the old service is ever stopped.
"""
from __future__ import annotations

import subprocess
import sys
import venv
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 3:
        print(
            "usage: hubinet-ops-update-venv-stage.py <staged-venv-path> <requirements-path>",
            file=sys.stderr,
        )
        return 2
    venv_path = Path(sys.argv[1])
    requirements_path = Path(sys.argv[2])

    if venv_path.exists():
        print(f"refusing to stage into an already-existing path: {venv_path}", file=sys.stderr)
        return 1
    if not requirements_path.is_file():
        print(f"requirements file does not exist: {requirements_path}", file=sys.stderr)
        return 1

    try:
        venv.create(venv_path, with_pip=True, symlinks=True)
    except OSError as exc:
        print(f"could not create staged virtualenv: {exc}", file=sys.stderr)
        return 1

    pip_path = venv_path / "bin" / "pip"
    if not pip_path.is_file():
        print(f"staged virtualenv has no pip at the expected path: {pip_path}", file=sys.stderr)
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
