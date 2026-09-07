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

from .enums import (
    HealthContractStatus,
    HealthDiscoveryAdapter,
    HealthDiscoveryOrigin,
    HealthDiscoveryRecommendationBasis,
    HealthDiscoveryRoleHint,
    HealthDiscoveryStatus,
    HealthProbeKind,
)
from .primitives import _require_enum_instance, _require_text, _require_uuid_identity

if TYPE_CHECKING:
    from .models import (
        HealthContractSummary,
        HealthDiscoveryCandidate,
        HealthDiscoveryResult,
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
    if probe.kind is HealthProbeKind.GUEST_OPERATIONAL:
        if probe.target is not None:
            raise ValueError(
                "a guest_operational health probe must not carry a target"
            )
        return
    if probe.target is None:
        raise ValueError("health probe target is required for this kind")
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


#: Bounds mirror the backend's own (deploy/hubinet-package-health-helper.py).
MAX_DISCOVERY_CANDIDATES = 128
_MAX_OBSERVED_STATE_LENGTH = 100
_MAX_RATIONALE_LENGTH = 100


#: The exact adapter -> probe-kind pairing. An adapter produces candidates of
#: its OWN kinds and no others, so `(adapter=systemd, kind=guest_operational)`
#: -- which would slip a targetless fallback in under a workload adapter, or
#: a workload probe in under the fallback adapter -- is refused rather than
#: rendered as if it described something real (PR #80 final review).
_DISCOVERY_ADAPTER_KINDS: dict[HealthDiscoveryAdapter, frozenset[HealthProbeKind]] = {
    HealthDiscoveryAdapter.DOCKER: frozenset(
        {
            HealthProbeKind.DOCKER_CONTAINER_RUNNING,
            HealthProbeKind.DOCKER_CONTAINER_HEALTHY,
        }
    ),
    HealthDiscoveryAdapter.SYSTEMD: frozenset({HealthProbeKind.SYSTEMD_UNIT_ACTIVE}),
    HealthDiscoveryAdapter.GUEST: frozenset({HealthProbeKind.GUEST_OPERATIONAL}),
}


def validate_health_discovery_candidate(candidate: "HealthDiscoveryCandidate") -> None:
    """Independent proof this is one coherent ephemeral candidate.

    Mirrors the backend's own coherence rules: an adapter only ever produces
    its own probe kinds, and ``target`` is ``None`` for, and only for, the
    ``guest`` adapter's ``guest_operational`` candidate -- never a faked
    target, and never a missing one for any other kind.
    """

    _require_enum_instance(
        candidate.adapter, HealthDiscoveryAdapter, "discovery candidate adapter"
    )
    _require_enum_instance(
        candidate.kind, HealthProbeKind, "discovery candidate kind"
    )
    if candidate.kind not in _DISCOVERY_ADAPTER_KINDS[candidate.adapter]:
        raise ValueError(
            "discovery candidate kind does not belong to its own adapter"
        )
    is_guest = candidate.kind is HealthProbeKind.GUEST_OPERATIONAL
    if is_guest:
        if candidate.target is not None:
            raise ValueError(
                "a guest_operational discovery candidate must not carry a target"
            )
    else:
        if candidate.target is None:
            raise ValueError("a discovery candidate target is required for this kind")
        _require_text(candidate.target, "discovery candidate target")
        if len(candidate.target) > MAX_HEALTH_PROBE_TARGET_LENGTH:
            raise ValueError("discovery candidate target is too long")
    _require_text(candidate.observed_state, "discovery candidate observed_state")
    if len(candidate.observed_state) > _MAX_OBSERVED_STATE_LENGTH:
        raise ValueError("discovery candidate observed_state is too long")
    if candidate.origin is not None:
        _require_enum_instance(
            candidate.origin, HealthDiscoveryOrigin, "discovery candidate origin"
        )
    _require_enum_instance(
        candidate.role_hint, HealthDiscoveryRoleHint, "discovery candidate role_hint"
    )
    if type(candidate.recommended) is not bool:
        raise ValueError("discovery candidate recommended must be a boolean")
    _require_text(candidate.rationale, "discovery candidate rationale")
    if len(candidate.rationale) > _MAX_RATIONALE_LENGTH:
        raise ValueError("discovery candidate rationale is too long")


def validate_health_discovery_result(result: "HealthDiscoveryResult") -> None:
    _require_uuid_identity(result.resource_id, "resource_id")
    _require_enum_instance(result.status, HealthDiscoveryStatus, "discovery status")
    if not isinstance(result.candidates, tuple):
        raise ValueError("discovery candidates must be a tuple")
    if len(result.candidates) > MAX_DISCOVERY_CANDIDATES:
        raise ValueError("discovery candidate count is out of bounds")
    from .models import HealthDiscoveryCandidate

    if not all(
        isinstance(candidate, HealthDiscoveryCandidate)
        for candidate in result.candidates
    ):
        raise ValueError(
            "discovery candidates must be a tuple of HealthDiscoveryCandidate"
        )
    if result.recommendation_basis is not None:
        _require_enum_instance(
            result.recommendation_basis,
            HealthDiscoveryRecommendationBasis,
            "discovery recommendation_basis",
        )
        if not any(candidate.recommended for candidate in result.candidates):
            raise ValueError(
                "a discovery recommendation_basis requires at least one "
                "recommended candidate"
            )
    elif any(candidate.recommended for candidate in result.candidates):
        raise ValueError(
            "a recommended discovery candidate requires a recommendation_basis"
        )
