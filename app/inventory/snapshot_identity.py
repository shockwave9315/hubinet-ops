"""Deterministic job-owned pre-update snapshot identity and ownership proof.

One package update job owns exactly one pre-update PVE snapshot. Both halves
of that snapshot's identity are derived here, purely, from immutable job
identity, so the exact same job derives the exact same identity after any
restart or retry:

- ``snapshot_name`` is the physical PVE key. It is bounded and
  collision-resistant but it is deliberately **not** an ownership proof: a
  name can be typed by anyone with PVE access.
- the structured ownership metadata carried in the snapshot's PVE
  *description* is the authority proof, and it is what later confirmation and
  rollback selection actually verify.

Verified PVE constraints this module is written against (Proxmox VE sources,
``pve-common`` ``PVE/JSONSchema.pm`` and ``pve-container``
``PVE/API2/LXC/Snapshot.pm``/``PVE/LXC/Config.pm``):

- ``pve-snapshot-name`` has format ``pve-configid`` = ``/^[a-z][a-z0-9_-]+$/i``
  with ``maxLength`` 40, so a name starts with a letter, is at least two
  characters long, and may contain only letters, digits, ``_`` and ``-``.
- ``current`` and ``vzdump`` are rejected by PVE as reserved snapshot names.
- a snapshot description is stored one ``#``-prefixed, percent-encoded line
  per line of text, and the config parser *appends* a newline to every line it
  reads back. A description therefore never round-trips byte-identically, so
  the parser here normalises line framing and whitespace before applying its
  strict checks.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import re
from typing import Any
import uuid

from .models import PackageUpdateSnapshotIdentity, SnapshotOwnership


#: PVE ``pve-configid`` format, which ``pve-snapshot-name`` builds on.
PVE_CONFIGID_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]+")
PVE_SNAPSHOT_NAME_MAX_LENGTH = 40
#: Snapshot names PVE itself refuses to create.
PVE_RESERVED_SNAPSHOT_NAMES = frozenset({"current", "vzdump"})

SNAPSHOT_NAME_PREFIX = "hubinet-preupd-"
_SNAPSHOT_NAME_DIGEST_LENGTH = 24

#: Structured-ownership protocol. Bump only with the parser and its tests.
SNAPSHOT_METADATA_PROTOCOL = 1
SNAPSHOT_METADATA_MARKER = "hubinet-ops-snapshot-v1"
#: Any line mentioning this token claims to be Hubinet metadata. A snapshot
#: carrying the token but no strictly parseable marker line is reported as
#: malformed rather than silently ignored, so ownership can never be
#: ambiguous.
SNAPSHOT_METADATA_TOKEN = "hubinet-ops-snapshot"
SNAPSHOT_KIND_PRE_UPDATE = "pre_update"
SNAPSHOT_DESCRIPTION_HEADLINE = (
    "Hubinet Ops job-owned pre-update snapshot - do not delete manually"
)
#: Generous ceiling for the two-line description this module emits. PVE
#: declares no maxLength for a snapshot description; this keeps the value
#: bounded on our side anyway.
SNAPSHOT_DESCRIPTION_MAX_LENGTH = 600

_NAME_DOMAIN = b"hubinet-ops/package-update/pre-update-snapshot-name/v1"
_OPERATION_DOMAIN = b"hubinet-ops/package-update/snapshot-operation-id/v1"

_OWNERSHIP_KEYS = frozenset(
    {
        "protocol",
        "kind",
        "job_id",
        "resource_id",
        "resource_continuity_revision",
        "inventory_source_id",
        "backend_instance_id",
    }
)


class SnapshotIdentityError(ValueError):
    """A snapshot identity or ownership value is not usable."""


def _canonical_uuid(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise SnapshotIdentityError(f"{field} must be a canonical UUID")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise SnapshotIdentityError(f"{field} must be a canonical UUID") from exc
    if parsed.int == 0 or str(parsed) != value:
        raise SnapshotIdentityError(
            f"{field} must be a canonical lowercase hyphenated non-NIL UUID"
        )
    return value


def _positive_integer(value: Any, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise SnapshotIdentityError(f"{field} must be a positive integer")
    return value


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(payload), ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")


def validate_pve_snapshot_name(value: Any) -> str:
    """Return ``value`` if PVE itself would accept it as a snapshot name."""

    if not isinstance(value, str):
        raise SnapshotIdentityError("snapshot name must be text")
    if (
        not PVE_CONFIGID_RE.fullmatch(value)
        or len(value) > PVE_SNAPSHOT_NAME_MAX_LENGTH
        or value.lower() in PVE_RESERVED_SNAPSHOT_NAMES
    ):
        raise SnapshotIdentityError("snapshot name is not a valid PVE snapshot name")
    return value


def _identity_payload(
    *,
    backend_instance_id: str,
    job_id: str,
    resource_id: str,
    resource_continuity_revision: int,
) -> bytes:
    return _canonical_json(
        {
            "backend_instance_id": _canonical_uuid(
                backend_instance_id, "backend_instance_id"
            ),
            "job_id": _canonical_uuid(job_id, "job_id"),
            "resource_id": _canonical_uuid(resource_id, "resource_id"),
            "resource_continuity_revision": _positive_integer(
                resource_continuity_revision, "resource_continuity_revision"
            ),
        }
    )


def derive_pre_update_snapshot_identity(
    *,
    backend_instance_id: str,
    job_id: str,
    resource_id: str,
    resource_continuity_revision: int,
) -> PackageUpdateSnapshotIdentity:
    """Derive one job's deterministic pre-update snapshot identity.

    Every input is an immutable authority fact, so this is stable across
    restarts and retries and never depends on the current time. Two different
    jobs -- including two jobs for the same resource incarnation -- derive
    different names, and the same job on a different backend installation
    derives a different name again.
    """

    payload = _identity_payload(
        backend_instance_id=backend_instance_id,
        job_id=job_id,
        resource_id=resource_id,
        resource_continuity_revision=resource_continuity_revision,
    )
    name_digest = hashlib.sha256(_NAME_DOMAIN + b"\n" + payload).hexdigest()
    snapshot_name = validate_pve_snapshot_name(
        SNAPSHOT_NAME_PREFIX + name_digest[:_SNAPSHOT_NAME_DIGEST_LENGTH]
    )
    operation_digest = hashlib.sha256(_OPERATION_DOMAIN + b"\n" + payload).digest()
    operation_id = str(uuid.UUID(bytes=operation_digest[:16], version=5))
    return PackageUpdateSnapshotIdentity(
        snapshot_operation_id=_canonical_uuid(operation_id, "snapshot_operation_id"),
        snapshot_name=snapshot_name,
    )


def build_snapshot_ownership(
    *,
    job_id: str,
    resource_id: str,
    resource_continuity_revision: int,
    inventory_source_id: str,
    backend_instance_id: str,
) -> SnapshotOwnership:
    """Build the strict ownership metadata for one job-owned snapshot."""

    return SnapshotOwnership(
        protocol=SNAPSHOT_METADATA_PROTOCOL,
        kind=SNAPSHOT_KIND_PRE_UPDATE,
        job_id=_canonical_uuid(job_id, "job_id"),
        resource_id=_canonical_uuid(resource_id, "resource_id"),
        resource_continuity_revision=_positive_integer(
            resource_continuity_revision, "resource_continuity_revision"
        ),
        inventory_source_id=_canonical_uuid(
            inventory_source_id, "inventory_source_id"
        ),
        backend_instance_id=_canonical_uuid(
            backend_instance_id, "backend_instance_id"
        ),
    )


def encode_snapshot_description(ownership: SnapshotOwnership) -> str:
    """Render one snapshot's PVE description: a headline plus a marker line.

    The payload is single-line, sorted, ASCII-only compact JSON, so it
    survives PVE's per-line percent-encoded description storage without any
    escaping surprises.
    """

    if not isinstance(ownership, SnapshotOwnership):
        raise SnapshotIdentityError("snapshot ownership metadata is required")
    payload = _canonical_json(
        {
            "protocol": _positive_integer(ownership.protocol, "protocol"),
            "kind": ownership.kind,
            "job_id": _canonical_uuid(ownership.job_id, "job_id"),
            "resource_id": _canonical_uuid(ownership.resource_id, "resource_id"),
            "resource_continuity_revision": _positive_integer(
                ownership.resource_continuity_revision,
                "resource_continuity_revision",
            ),
            "inventory_source_id": _canonical_uuid(
                ownership.inventory_source_id, "inventory_source_id"
            ),
            "backend_instance_id": _canonical_uuid(
                ownership.backend_instance_id, "backend_instance_id"
            ),
        }
    ).decode("ascii")
    if ownership.kind != SNAPSHOT_KIND_PRE_UPDATE:
        raise SnapshotIdentityError("snapshot ownership kind is not pre-update")
    if ownership.protocol != SNAPSHOT_METADATA_PROTOCOL:
        raise SnapshotIdentityError("snapshot ownership protocol is unsupported")
    description = f"{SNAPSHOT_DESCRIPTION_HEADLINE}\n{SNAPSHOT_METADATA_MARKER} {payload}"
    if len(description) > SNAPSHOT_DESCRIPTION_MAX_LENGTH:
        raise SnapshotIdentityError("snapshot description exceeds its bound")
    if any(ord(character) > 0x7E or ord(character) < 0x20 for character in payload):
        raise SnapshotIdentityError("snapshot description payload must be ASCII text")
    return description


def looks_like_hubinet_snapshot(description: Any) -> bool:
    """Report whether a description claims, in any form, to be Hubinet's."""

    return isinstance(description, str) and SNAPSHOT_METADATA_TOKEN in description


def parse_snapshot_description(description: Any) -> SnapshotOwnership | None:
    """Strictly parse Hubinet ownership metadata out of a PVE description.

    Returns ``None`` when the description makes no Hubinet ownership claim at
    all -- a foreign or manual snapshot. Raises :class:`SnapshotIdentityError`
    when it *does* look like a Hubinet snapshot but the metadata is malformed,
    incomplete, duplicated, or otherwise not exactly one well-formed claim, so
    a caller can fail closed instead of silently skipping it.
    """

    if not looks_like_hubinet_snapshot(description):
        return None
    marker_lines = [
        line.strip()
        for line in description.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if line.strip().startswith(SNAPSHOT_METADATA_MARKER)
    ]
    if len(marker_lines) != 1:
        raise SnapshotIdentityError(
            "snapshot description does not carry exactly one Hubinet marker line"
        )
    remainder = marker_lines[0][len(SNAPSHOT_METADATA_MARKER):]
    if not remainder.startswith(" "):
        raise SnapshotIdentityError("snapshot marker line is malformed")
    try:
        payload = json.loads(remainder.strip())
    except ValueError as exc:
        raise SnapshotIdentityError(
            "snapshot ownership metadata is not valid JSON"
        ) from exc
    if not isinstance(payload, dict) or set(payload) != _OWNERSHIP_KEYS:
        raise SnapshotIdentityError(
            "snapshot ownership metadata does not have the exact expected shape"
        )
    if payload["protocol"] != SNAPSHOT_METADATA_PROTOCOL:
        raise SnapshotIdentityError("snapshot ownership protocol is unsupported")
    if payload["kind"] != SNAPSHOT_KIND_PRE_UPDATE:
        raise SnapshotIdentityError("snapshot ownership kind is not pre-update")
    return SnapshotOwnership(
        protocol=SNAPSHOT_METADATA_PROTOCOL,
        kind=SNAPSHOT_KIND_PRE_UPDATE,
        job_id=_canonical_uuid(payload["job_id"], "job_id"),
        resource_id=_canonical_uuid(payload["resource_id"], "resource_id"),
        resource_continuity_revision=_positive_integer(
            payload["resource_continuity_revision"], "resource_continuity_revision"
        ),
        inventory_source_id=_canonical_uuid(
            payload["inventory_source_id"], "inventory_source_id"
        ),
        backend_instance_id=_canonical_uuid(
            payload["backend_instance_id"], "backend_instance_id"
        ),
    )
