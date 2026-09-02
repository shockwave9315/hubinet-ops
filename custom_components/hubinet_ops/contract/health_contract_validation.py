"""Health-contract portions of the Hubinet Ops snapshot and action contract.

Two separate shapes live here and must not be confused:

- ``HealthContractSummary`` is what the published *snapshot* carries per
  resource -- whether a declared meaning of healthy exists, and its identity.
  It never carries probes.
- ``ResourceHealthContract`` is the full contract material, returned only by
  the dedicated health-contract action/endpoint that an operator explicitly
  invokes.

Both are configuration. Neither is, or may become, a health result: this
integration has no health-result state to validate because the backend has no
health execution to produce one.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .enums import HealthContractStatus, HealthProbeKind
from .primitives import _require_enum_instance, _require_text

if TYPE_CHECKING:
    from .models import (
        HealthContractSummary,
        HealthProbe,
        ResourceHealthContract,
    )

#: Mirrors the backend bound (`app/inventory/health_contract.py`). Home
#: Assistant validates it independently rather than trusting the payload:
#: this contract layer's job is to refuse a backend response that is outside
#: the agreed shape, not to render whatever arrives.
MAX_HEALTH_PROBES = 32
MAX_HEALTH_PROBE_TARGET_LENGTH = 200

_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")


def validate_health_probe(probe: "HealthProbe") -> None:
    _require_enum_instance(probe.kind, HealthProbeKind, "health probe kind")
    _require_text(probe.target, "health probe target")
    if len(probe.target) > MAX_HEALTH_PROBE_TARGET_LENGTH:
        raise ValueError("health probe target is too long")
    if any(character.isspace() for character in probe.target):
        raise ValueError("health probe target must not contain whitespace")


def validate_health_contract_summary(summary: "HealthContractSummary") -> None:
    _require_enum_instance(
        summary.status, HealthContractStatus, "health_contract.status"
    )
    material = (
        summary.revision,
        summary.fingerprint,
        summary.probe_count,
        summary.updated_at,
    )
    if summary.status is not HealthContractStatus.CONFIGURED:
        # `unconfigured` and `unsupported` mean there is nothing to describe.
        # A summary carrying identity fields for a resource with no contract
        # would be a claim about a contract that does not exist.
        if any(value is not None for value in material):
            raise ValueError(
                "a resource with no health contract has no contract identity"
            )
        return

    if any(value is None for value in material):
        raise ValueError("a configured health contract requires all identity fields")
    if type(summary.revision) is not int or summary.revision <= 0:
        raise ValueError("health_contract.revision must be a positive integer")
    if not isinstance(summary.fingerprint, str) or not _FINGERPRINT_RE.fullmatch(
        summary.fingerprint
    ):
        raise ValueError("health_contract.fingerprint is malformed")
    if (
        type(summary.probe_count) is not int
        or not 1 <= summary.probe_count <= MAX_HEALTH_PROBES
    ):
        raise ValueError("health_contract.probe_count is out of bounds")
    _require_text(summary.updated_at, "health_contract.updated_at")


def validate_resource_health_contract(contract: "ResourceHealthContract") -> None:
    from .models import HealthProbe

    _require_enum_instance(
        contract.status, HealthContractStatus, "health contract status"
    )
    material = (
        contract.revision,
        contract.fingerprint,
        contract.created_at,
        contract.updated_at,
        contract.probes,
    )
    if contract.status is not HealthContractStatus.CONFIGURED:
        # `probes` is None, never (), for an unconfigured resource. An empty
        # tuple would read as "a contract that requires nothing", which is
        # exactly the false reassurance this product refuses to publish.
        if any(value is not None for value in material):
            raise ValueError("an absent health contract has no contract material")
        return

    if any(value is None for value in material):
        raise ValueError("a configured health contract requires all its material")
    if type(contract.revision) is not int or contract.revision <= 0:
        raise ValueError("health contract revision must be a positive integer")
    if not isinstance(contract.fingerprint, str) or not _FINGERPRINT_RE.fullmatch(
        contract.fingerprint
    ):
        raise ValueError("health contract fingerprint is malformed")
    _require_text(contract.created_at, "health contract created_at")
    _require_text(contract.updated_at, "health contract updated_at")
    probes = contract.probes
    if not isinstance(probes, tuple) or not all(
        isinstance(probe, HealthProbe) for probe in probes
    ):
        raise ValueError("health contract probes must be a tuple of HealthProbe")
    if not 1 <= len(probes) <= MAX_HEALTH_PROBES:
        raise ValueError("health contract probe count is out of bounds")
    identities = {(probe.kind, probe.target) for probe in probes}
    if len(identities) != len(probes):
        raise ValueError("health contract contains a duplicate probe")
