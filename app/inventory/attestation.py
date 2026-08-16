"""Trusted, injected boundary for source-attestation remote evidence reads.

ADR 0003 §19a requires every attestation-gated remote evidence read to happen
entirely outside any authority DB write transaction, and §29's fail-closed
rules require that tier-2 cryptographic evidence can only ever be produced by
a trusted verifier boundary -- never accepted as a raw caller-supplied
boolean. This module defines that boundary as a narrow typed protocol so
tests can supply deterministic evidence and race behavior, and so a future
runtime package can supply a real implementation (real network I/O plus, if
implemented, real X.509 chain verification against the enrolled PVE root CA)
without changing any authority call site.

This module ships no production network/TLS implementation. Tier 2 remains
optional, corroborating evidence (ADR 0003 §10a); a reader that cannot
evaluate it must report ``tier2_verified=None`` ("not evaluated"), never a
fabricated True/False.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


#: The one anchor kind ADR 0003 §9 recommends: the PVE root CA
#: (``/etc/pve/pve-root-ca.pem``) SHA-256 fingerprint, read as ordinary API
#: response data. No tier-3 primitive exists; this is the only supported
#: anchor kind in this wave.
ANCHOR_KIND_PVE_ROOT_CA_SHA256_FINGERPRINT = "pve_root_ca_sha256_fingerprint"


class SourceAttestationReadOutcome(StrEnum):
    """What one remote evidence-read attempt produced, before any comparison
    against a currently enrolled anchor. The match/mismatch decision itself
    is authority-owned (it requires the currently enrolled value, which the
    reader is not trusted to decide), not part of this outcome."""

    OBSERVED = "observed"
    UNAVAILABLE = "unavailable"
    MALFORMED = "malformed"


@dataclass(frozen=True, slots=True)
class SourceAttestationEvidenceReading:
    """Result of one remote tier-1 (+ optional tier-2) evidence read.

    Produced entirely outside any authority DB write transaction (ADR 0003
    §19a step 2). ``tier2_verified`` may be set to ``True``/``False`` only by
    a genuine :class:`SourceAttestationEvidenceReader` implementation that
    actually performed X.509 chain verification against the enrolled PVE
    root CA; it is never accepted from an untrusted caller-supplied value by
    any authority method.
    """

    outcome: SourceAttestationReadOutcome
    anchor_kind: str | None = None
    anchor_value: str | None = None
    tier2_verified: bool | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, SourceAttestationReadOutcome):
            raise ValueError("evidence read outcome must be canonical")
        if self.outcome is SourceAttestationReadOutcome.OBSERVED:
            if (
                not isinstance(self.anchor_kind, str)
                or not self.anchor_kind.strip()
                or not isinstance(self.anchor_value, str)
                or not self.anchor_value.strip()
            ):
                raise ValueError(
                    "an observed evidence reading must include a non-empty asserted anchor"
                )
        else:
            if self.anchor_kind is not None or self.anchor_value is not None:
                raise ValueError(
                    "unavailable/malformed evidence must not assert an anchor value"
                )
            if self.tier2_verified is not None:
                raise ValueError(
                    "unavailable/malformed evidence must not carry a tier-2 result"
                )
        if self.tier2_verified is not None and type(self.tier2_verified) is not bool:
            raise ValueError("tier2_verified must be boolean or unknown (None)")


class SourceAttestationEvidenceReader(Protocol):
    """Trusted boundary that performs the remote tier-1(+tier-2) evidence read.

    Implementations own the actual network I/O and any tier-2 TLS
    peer-certificate-chain verification against the already-enrolled PVE
    root CA. This wave ships no production implementation of this protocol
    -- only the typed, injectable seam -- so that real network/TLS wiring
    stays outside the dormant Phase 1 backend until a separately reviewed
    runtime package adds it. Test doubles implement this protocol directly;
    authority methods never accept a raw evidence boolean from a caller.
    """

    def read(
        self,
        *,
        inventory_source_id: str,
        endpoint_id: str,
        canonical_transport_locator: str,
        enrolled_anchor_kind: str | None,
        enrolled_anchor_value: str | None,
    ) -> SourceAttestationEvidenceReading: ...


__all__ = [
    "ANCHOR_KIND_PVE_ROOT_CA_SHA256_FINGERPRINT",
    "SourceAttestationEvidenceReader",
    "SourceAttestationEvidenceReading",
    "SourceAttestationReadOutcome",
]
