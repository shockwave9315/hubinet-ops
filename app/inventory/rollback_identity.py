"""Deterministic job-owned same-job rollback operation identity.

One package update job may cause AT MOST ONE PVE snapshot rollback, and only
ever to the snapshot that exact job created and confirmed. That operation's
identity is derived here, purely, from immutable authority, so the exact same
job derives the exact same operation id after any restart or retry -- which is
what lets a recovering backend reattach to the host's durable at-most-once
journal instead of guessing whether `pvesh create .../rollback` already ran.

This is the rollback analogue of `snapshot_identity.py` and
`mutation_identity.py`, and deliberately a separate module with its own
domain separator. The bound facts are:

- ``backend_instance_id`` -- this Hubinet installation;
- ``job_id`` -- this one update job;
- ``resource_id`` -- this durable resource identity, never a VMID;
- ``resource_continuity_revision`` -- this exact incarnation of it;
- ``snapshot_operation_id`` and ``snapshot_name`` -- the job's own CONFIRMED
  snapshot, i.e. the exact thing being rolled back TO.

The snapshot half is included even though it is itself derived from the first
four facts today. It is not redundant defence: it makes the operation id mean
"roll THIS job back to THIS exact snapshot", so that if the snapshot identity
contract ever changes, a rollback aimed at a different snapshot is
structurally a different operation with a different journal, rather than one
that silently reuses an existing record. `AGENTS.md` and `PRODUCT.md` require
rollback to target only the job's own snapshot; this binds that requirement
into the operation identity itself.

The VMID, node, binding, and locator generation are deliberately NOT inputs:
they are execution locators that may legitimately change while the same job's
identity does not, and they are fenced separately, per request, by the host
boundary's own request fingerprint and live target revalidation.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from typing import Any
import uuid

from .models import PackageUpdateRollbackIdentity


class RollbackIdentityError(ValueError):
    """A same-job rollback identity input is not usable."""


#: Domain-separated from every other digest in this repository, so a rollback
#: operation id can never collide with, or be mistaken for, the snapshot
#: operation id or the package mutation operation id derived from the exact
#: same job facts.
_OPERATION_DOMAIN = b"hubinet-ops/package-update/rollback-operation-id/v1"


def _canonical_uuid(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise RollbackIdentityError(f"{field} must be a canonical UUID")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise RollbackIdentityError(f"{field} must be a canonical UUID") from exc
    if parsed.int == 0 or str(parsed) != value:
        raise RollbackIdentityError(
            f"{field} must be a canonical lowercase hyphenated non-NIL UUID"
        )
    return value


def _positive_integer(value: Any, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise RollbackIdentityError(f"{field} must be a positive integer")
    return value


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(payload), ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")


def derive_package_rollback_identity(
    *,
    backend_instance_id: str,
    job_id: str,
    resource_id: str,
    resource_continuity_revision: int,
    snapshot_operation_id: str,
    snapshot_name: str,
) -> PackageUpdateRollbackIdentity:
    """Derive one job's deterministic same-job rollback operation identity.

    Every input is an immutable authority fact, so this is stable across
    restarts and retries and never depends on the current time, on a VMID, or
    on anything a caller may choose. There is deliberately no way to supply an
    operation id: the host boundary refuses any request whose id it cannot
    bind to the exact request fingerprint it already journaled.
    """

    if not isinstance(snapshot_name, str) or not snapshot_name:
        raise RollbackIdentityError("snapshot_name must be a non-empty string")
    payload = _canonical_json(
        {
            "backend_instance_id": _canonical_uuid(
                backend_instance_id, "backend_instance_id"
            ),
            "job_id": _canonical_uuid(job_id, "job_id"),
            "resource_id": _canonical_uuid(resource_id, "resource_id"),
            "resource_continuity_revision": _positive_integer(
                resource_continuity_revision, "resource_continuity_revision"
            ),
            "snapshot_operation_id": _canonical_uuid(
                snapshot_operation_id, "snapshot_operation_id"
            ),
            "snapshot_name": snapshot_name,
        }
    )
    digest = hashlib.sha256(_OPERATION_DOMAIN + b"\n" + payload).digest()
    operation_id = str(uuid.UUID(bytes=digest[:16], version=5))
    return PackageUpdateRollbackIdentity(
        rollback_operation_id=_canonical_uuid(
            operation_id, "rollback_operation_id"
        )
    )
