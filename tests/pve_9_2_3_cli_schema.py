"""Narrow PVE 9.2.3 ``pvesh`` argv/schema oracle for helper tests.

This models only the endpoints used by the package snapshot and rollback
helpers.  The grammar and endpoint properties come from these pinned upstream
source revisions used for the regression:

* pve-manager ``d0fde10`` (9.2.3), ``PVE/CLI/pvesh.pm``: ``--noproxy`` is
  consumed only as a leading global compatibility option, before the verb;
* pve-container ``2cef17b`` (6.1.3), ``PVE/API2/LXC/Snapshot.pm``: snapshot
  POST accepts ``snapname`` and ``description`` beyond path parameters, and
  rollback POST accepts only ``start`` beyond path parameters.  Both schemas
  set ``additionalProperties => 0``.

It is intentionally not a general Proxmox emulator.  Its purpose is to make
the fake boundary reject the same unsupported API properties real PVE rejects.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Sequence


class PveshSchemaError(ValueError):
    """The argv cannot be represented by the pinned PVE CLI/API schema."""


@dataclass(frozen=True, slots=True)
class PveshCall:
    verb: str
    path: str
    noproxy: bool
    parameters: dict[str, str]
    output_format: str | None


@dataclass(frozen=True, slots=True)
class _Endpoint:
    verb: str
    path: re.Pattern[str]
    parameters: frozenset[str]
    required: frozenset[str] = frozenset()


_ENDPOINTS = (
    _Endpoint("get", re.compile(r"/cluster/status"), frozenset()),
    _Endpoint("get", re.compile(r"/cluster/resources"), frozenset({"type"})),
    _Endpoint(
        "get",
        re.compile(r"/nodes/[^/]+/lxc/[0-9]+/config"),
        frozenset(),
    ),
    _Endpoint(
        "get",
        re.compile(r"/nodes/[^/]+/lxc/[0-9]+/snapshot"),
        frozenset(),
    ),
    _Endpoint(
        "get",
        re.compile(r"/nodes/[^/]+/tasks/[^/]+/status"),
        frozenset(),
    ),
    _Endpoint(
        "create",
        re.compile(r"/nodes/[^/]+/lxc/[0-9]+/snapshot"),
        frozenset({"snapname", "description"}),
        frozenset({"snapname"}),
    ),
    _Endpoint(
        "create",
        re.compile(r"/nodes/[^/]+/lxc/[0-9]+/snapshot/[^/]+/rollback"),
        frozenset({"start"}),
    ),
)


def parse_pvesh_9_2_3(argv: Sequence[str]) -> PveshCall:
    """Parse one helper argv exactly where PVE separates CLI and API options."""

    values = tuple(argv)
    if not values or values[0] != "pvesh":
        raise PveshSchemaError("command is not pvesh")

    cursor = 1
    noproxy = False
    while cursor < len(values) and values[cursor] == "--noproxy":
        if noproxy:
            raise PveshSchemaError("noproxy: global option was specified more than once")
        noproxy = True
        cursor += 1

    if cursor >= len(values) or values[cursor] not in {"get", "create"}:
        raise PveshSchemaError("unsupported or missing pvesh verb")
    verb = values[cursor]
    cursor += 1
    if cursor >= len(values) or not values[cursor].startswith("/"):
        raise PveshSchemaError("missing pvesh API path")
    path = values[cursor]
    cursor += 1

    endpoint = next(
        (
            candidate
            for candidate in _ENDPOINTS
            if candidate.verb == verb and candidate.path.fullmatch(path)
        ),
        None,
    )
    if endpoint is None:
        raise PveshSchemaError(f"no {verb!r} schema fixture for {path!r}")

    parameters: dict[str, str] = {}
    output_format: str | None = None
    while cursor < len(values):
        option = values[cursor]
        cursor += 1
        if not option.startswith("--") or cursor >= len(values):
            raise PveshSchemaError("pvesh options must be name/value pairs")
        name = option[2:]
        value = values[cursor]
        cursor += 1
        if name == "output-format":
            if output_format is not None:
                raise PveshSchemaError("output-format: option was specified more than once")
            if value != "json":
                raise PveshSchemaError("output-format: helper fixture requires json")
            output_format = value
            continue
        if name not in endpoint.parameters:
            raise PveshSchemaError(
                f"{name}: property is not defined in schema and the schema "
                "does not allow additional properties"
            )
        if name in parameters:
            raise PveshSchemaError(f"{name}: property was specified more than once")
        parameters[name] = value

    missing = endpoint.required.difference(parameters)
    if missing:
        raise PveshSchemaError(
            f"{sorted(missing)[0]}: property is missing and it is not optional"
        )
    return PveshCall(verb, path, noproxy, parameters, output_format)
