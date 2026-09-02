"""Pure structural eligibility rules for package-update health execution.

Resource health contracts deliberately store bounded opaque targets.  The dark
package-update executor is narrower: it can only evaluate exact systemd unit
names and exact Docker container names.  This module is that execution-only
boundary.  It performs no host I/O and does not change what the configuration
layer may store.
"""

from __future__ import annotations

from collections.abc import Iterable
import re

from .models import HealthProbeKind, ResourceHealthProbe


class HealthContractExecutionError(ValueError):
    """A stored contract cannot be represented by the exact executor."""


# These patterns are mirrored by the standalone PVE-host helper, which cannot
# import backend application code.  A regression test compares the helper's
# compiled patterns and suffix set byte-for-byte with these definitions.
SYSTEMD_UNIT_PATTERN = r"[A-Za-z0-9][A-Za-z0-9:_.@-]{0,199}"
SYSTEMD_UNIT_SUFFIXES = (
    ".service",
    ".socket",
    ".target",
    ".timer",
    ".mount",
    ".automount",
    ".path",
    ".slice",
    ".scope",
    ".device",
    ".swap",
)
DOCKER_NAME_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}"

_SYSTEMD_UNIT_RE = re.compile(SYSTEMD_UNIT_PATTERN)
_DOCKER_NAME_RE = re.compile(DOCKER_NAME_PATTERN)


def require_health_contract_execution_eligible(
    probes: Iterable[ResourceHealthProbe],
) -> None:
    """Require every probe to be exactly representable by the executor.

    This is structural validation only.  It never asks whether a unit or
    container currently exists, is running, or is healthy.
    """

    for probe in probes:
        if probe.kind is HealthProbeKind.SYSTEMD_UNIT_ACTIVE:
            if (
                not _SYSTEMD_UNIT_RE.fullmatch(probe.target)
                or probe.target.startswith("-")
                or not probe.target.endswith(SYSTEMD_UNIT_SUFFIXES)
            ):
                raise HealthContractExecutionError(
                    "systemd health probe target is not an exact explicit unit name"
                )
            continue
        if probe.kind in (
            HealthProbeKind.DOCKER_CONTAINER_RUNNING,
            HealthProbeKind.DOCKER_CONTAINER_HEALTHY,
        ):
            if (
                not _DOCKER_NAME_RE.fullmatch(probe.target)
                or probe.target.startswith("-")
            ):
                raise HealthContractExecutionError(
                    "Docker health probe target is not an exact container name"
                )
            continue
        raise HealthContractExecutionError(
            "health probe kind is not supported by the package-update executor"
        )
