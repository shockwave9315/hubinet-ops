"""Operator-declared per-resource workload health contracts.

A health contract is CONFIGURATION, not a result. It says what "healthy"
means for one exact dynamic resource incarnation, and nothing here executes,
schedules, or interprets a probe: this module only canonicalizes, validates,
and fingerprints the operator's declaration so the durable authority row is
bounded and deterministic.

The three rules that shape everything below:

- **Health is operator-declared per `resource_id`.** Never per VMID, per
  hostname, per node, and never from a repository or config file. A VMID-reused
  replacement is a different resource incarnation and inherits nothing.
- **All configured probes are required.** There is no OR tree, no scoring, no
  percentage, and no boolean expression -- exactly an AND over the declared
  set. That is why a probe set needs no structure beyond a canonical ordering.
- **Absence is not health.** No contract means *unconfigured*, never "passing".
  An empty probe set is therefore not a contract, it is a malformed one, and
  is rejected here rather than stored.

A probe target is DATA. The executor uses fixed argv operations, so a target is
never command text and this configuration module deliberately does not
implement systemd or Docker execution grammar. Structural execution
eligibility is a separate pure check in ``health_execution.py`` at package-job
issuance. This layer only enforces that a target cannot stop being one bounded
opaque argument: no NUL, no control character, no whitespace, no unbounded
length.
"""

from __future__ import annotations

from collections.abc import Iterable
import hashlib
import json
import unicodedata

from .models import HealthProbeKind, ResourceHealthProbe


class HealthContractError(ValueError):
    """An operator-supplied health contract declaration is not usable."""


#: Bounds. A contract is one operator's short list of the things that must be
#: true for a workload to be considered up, not an inventory of the guest.
MIN_HEALTH_PROBES = 1
MAX_HEALTH_PROBES = 32

#: A systemd unit name and a Docker container name are both far shorter than
#: this in practice; the bound exists to keep the durable row bounded, not to
#: model either grammar.
MAX_HEALTH_PROBE_TARGET_LENGTH = 200

#: Domain-separated from every other digest in this repository so a health
#: contract fingerprint can never collide with, or be mistaken for, an
#: approved package plan fingerprint.
_FINGERPRINT_DOMAIN = "hubinet-ops/resource-health-contract/v1"


def _require_probe_target(value: object) -> str:
    if not isinstance(value, str):
        raise HealthContractError("health probe target must be a string")
    if not value:
        raise HealthContractError("health probe target must not be empty")
    if len(value) > MAX_HEALTH_PROBE_TARGET_LENGTH:
        raise HealthContractError(
            "health probe target exceeds "
            f"{MAX_HEALTH_PROBE_TARGET_LENGTH} characters"
        )
    for character in value:
        if character.isspace():
            # Covers ordinary spaces, tabs, newlines, and every Unicode
            # separator. A unit or container name never contains one, and a
            # target that did could not stay one bounded opaque argument.
            raise HealthContractError(
                "health probe target must not contain whitespace"
            )
        category = unicodedata.category(character)
        if category in {"Cc", "Cf", "Cs", "Co", "Cn"}:
            # Cc catches NUL and every other C0/C1 control; Cf catches
            # bidirectional and other invisible formatting marks; the rest
            # catch surrogates, private use, and unassigned code points.
            raise HealthContractError(
                "health probe target must not contain control characters"
            )
    return value


def canonical_health_probes(
    probes: Iterable[ResourceHealthProbe],
) -> tuple[ResourceHealthProbe, ...]:
    """Validate and canonically order one complete declared probe set.

    Canonical order is ``(kind, target)`` ascending, so two operators who
    declare the same probes in different orders declare the same contract and
    produce the same fingerprint. Duplicate ``(kind, target)`` pairs are
    rejected rather than deduplicated: an "all of these" contract that lists
    the same requirement twice is a mistake worth reporting, not a shape to
    silently repair.
    """

    if isinstance(probes, (str, bytes)) or not isinstance(probes, Iterable):
        raise HealthContractError("health probes must be a sequence")
    normalized: list[ResourceHealthProbe] = []
    identities: set[tuple[str, str]] = set()
    for probe in probes:
        if not isinstance(probe, ResourceHealthProbe):
            raise HealthContractError(
                "health probes must contain ResourceHealthProbe values"
            )
        if not isinstance(probe.kind, HealthProbeKind):
            raise HealthContractError("health probe kind is not supported")
        target = _require_probe_target(probe.target)
        identity = (probe.kind.value, target)
        if identity in identities:
            raise HealthContractError(
                "health contract contains a duplicate (kind, target) probe"
            )
        identities.add(identity)
        normalized.append(ResourceHealthProbe(kind=probe.kind, target=target))

    if len(normalized) < MIN_HEALTH_PROBES:
        # An empty contract is invalid, never "nothing to check, so healthy".
        raise HealthContractError(
            "a health contract requires at least one probe; "
            "clear the contract instead of declaring an empty one"
        )
    if len(normalized) > MAX_HEALTH_PROBES:
        raise HealthContractError(
            f"a health contract may declare at most {MAX_HEALTH_PROBES} probes"
        )
    return tuple(sorted(normalized, key=lambda probe: (probe.kind.value, probe.target)))


def health_contract_fingerprint(probes: Iterable[ResourceHealthProbe]) -> str:
    """SHA-256 over the canonical contract material only.

    The material is exactly the canonically ordered ``(kind, target)`` set --
    the thing a future executor would have to satisfy. Request ordering never
    affects it, and neither do the row's provenance fields (``resource_id``,
    ``revision``, timestamps): two resources that require the same probes have
    the same fingerprint, exactly as two identical package plans do. That is
    what lets a later health-execution stage say "this run evaluated contract
    fingerprint X" and mean something checkable.
    """

    canonical = canonical_health_probes(probes)
    payload = {
        "domain": _FINGERPRINT_DOMAIN,
        "probes": [
            {"kind": probe.kind.value, "target": probe.target}
            for probe in canonical
        ],
    }
    encoded = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
