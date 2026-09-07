"""Per-resource workload health contracts.

A health contract is CONFIGURATION, not a result. It says what "healthy"
means for one exact dynamic resource incarnation, and nothing here executes,
schedules, or interprets a probe: this module only canonicalizes, validates,
and fingerprints a declaration so the durable authority row is bounded and
deterministic.

Two things declare one, and the split is the whole v0.5 product decision.
The BASELINE is BACKEND-OWNED: every current package-managed LXC is
provisioned `DEFAULT_HEALTH_PROBES` below, as product policy. An ADVANCED
contract is OPERATOR-DECLARED: an operator may explicitly replace that
baseline with one or more named `systemd_unit_active` probes, and it is then
theirs until they explicitly reset it. Neither half is INFERRED -- nothing
anywhere reads a guest to decide what its contract should be. v0.5 dropped
Docker-specific package-update health probes entirely; Docker workload health
is not part of v0.5 Hubinet Ops package-update health.

The rules that shape everything below:

- **A contract belongs to one exact `resource_id`.** Never a VMID, a
  hostname, a node, or a repository/config file, and never derived from what
  a guest appears to run. A VMID-reused replacement is a different resource
  incarnation and inherits nothing.
- **All configured probes are required.** There is no OR tree, no scoring, no
  percentage, and no boolean expression -- exactly an AND over the declared
  set. That is why a probe set needs no structure beyond a canonical ordering.
- **Absence is not health.** No contract means *unconfigured*, never "passing".
  An empty probe set is therefore not a contract, it is a malformed one, and
  is rejected here rather than stored.
- **A managed LXC gets a built-in default rather than staying unconfigured.**
  `DEFAULT_HEALTH_PROBES` below is the v0.5 baseline the backend provisions
  for every current package-managed LXC. It is a product decision, not an
  observation of the guest: nothing here or anywhere else inspects a guest to
  choose it, because absence of a workload observer is not proof of workload
  absence. An operator's own explicit contract always wins and is never
  overwritten by it.
- **The baseline and an advanced contract never mix.** A contract is either
  exactly one `guest_operational` probe (target `NULL`) or one-or-more
  `systemd_unit_active` probes -- never both in the same contract. An
  explicit advanced contract REPLACES the baseline; it does not extend it,
  and mixing them would silently reintroduce the settling coupling this
  design deliberately removed from the baseline (see
  `evaluate_health_contract_settling` in the deployed health helper).

A probe target is DATA. The executor uses fixed argv operations, so a target is
never command text and this configuration module deliberately does not
implement systemd execution grammar. Structural execution
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

#: A systemd unit name is far shorter than this in practice; the bound exists
#: to keep the durable row bounded, not to model its grammar.
MAX_HEALTH_PROBE_TARGET_LENGTH = 200

#: Domain-separated from every other digest in this repository so a health
#: contract fingerprint can never collide with, or be mistaken for, an
#: approved package plan fingerprint.
_FINGERPRINT_DOMAIN = "hubinet-ops/resource-health-contract/v1"


def _require_probe_target(kind: HealthProbeKind, value: object) -> str | None:
    """A target is DATA for every kind except one.

    ``GUEST_OPERATIONAL`` names no container or unit -- it MUST be ``None``,
    never a faked placeholder (``"guest"``, ``"/bin/true"``, a VMID string).
    Every other kind requires a real bounded opaque-argument string.
    """

    if kind is HealthProbeKind.GUEST_OPERATIONAL:
        if value is not None:
            raise HealthContractError(
                "guest_operational health probes must not carry a target"
            )
        return None
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
    identities: set[tuple[str, str | None]] = set()
    for probe in probes:
        if not isinstance(probe, ResourceHealthProbe):
            raise HealthContractError(
                "health probes must contain ResourceHealthProbe values"
            )
        if not isinstance(probe.kind, HealthProbeKind):
            raise HealthContractError("health probe kind is not supported")
        target = _require_probe_target(probe.kind, probe.target)
        identity = (probe.kind.value, target)
        if identity in identities:
            # For GUEST_OPERATIONAL specifically, this is also the "at most
            # one" rule: every such probe has the identical (kind, None)
            # identity, so a second one is always a duplicate.
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
    if (
        len(normalized) > 1
        and any(probe.kind is HealthProbeKind.GUEST_OPERATIONAL for probe in normalized)
    ):
        # The built-in baseline and an explicit advanced contract never mix
        # (PRODUCT.md, "What healthy means"). An advanced contract REPLACES
        # the baseline; declaring GUEST_OPERATIONAL alongside anything else
        # would silently reintroduce the settling coupling the baseline is
        # deliberately free of.
        raise HealthContractError(
            "guest_operational may not be combined with any other probe; "
            "an explicit advanced contract replaces the baseline entirely"
        )
    return tuple(
        sorted(normalized, key=lambda probe: (probe.kind.value, probe.target or ""))
    )


#: The v0.5 BUILT-IN DEFAULT contract for a package-managed LXC.
#:
#: One probe, no target: "the exact current LXC remained reachable through
#: the trusted PVE boundary and successfully executed the fixed code-owned
#: command". It is a PRODUCT DEFAULT owned by the backend, not an inference
#: about what the guest runs -- absence of a workload observer is not proof
#: of workload absence, so v0.5 does not infer workload health
#: automatically. `systemd_unit_active` remains available, but only as an
#: explicit operator-declared advanced contract that REPLACES this baseline
#: entirely (never mixed with it). Docker workload health is not part of
#: v0.5 Hubinet Ops package-update health.
DEFAULT_HEALTH_PROBES: tuple[ResourceHealthProbe, ...] = (
    ResourceHealthProbe(kind=HealthProbeKind.GUEST_OPERATIONAL, target=None),
)


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


#: Precomputed once from `DEFAULT_HEALTH_PROBES`, so the default-provisioning
#: path never has to re-derive it per resource and a test can assert against
#: the exact same material the provisioner writes.
DEFAULT_HEALTH_CONTRACT_FINGERPRINT: str = health_contract_fingerprint(
    DEFAULT_HEALTH_PROBES
)
